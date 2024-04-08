# Copyright OTT-JAX
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from typing import NamedTuple, Optional, Tuple, Union

import jax
import jax.numpy as jnp
import lineax as lx

from ott import utils
from ott.geometry import costs, pointcloud
from ott.math import utils as mu
from ott.problems.linear import linear_problem

__all__ = [
    "UnivariateOutput", "UnivariateSolver", "uniform_distance",
    "quantile_distance"
]

Distance_t = Tuple[float, Optional[jnp.ndarray], Optional[jnp.ndarray]]


class UnivariateOutput(NamedTuple):  # noqa: D101
  """Output of the :class:`~ott.solvers.linear.UnivariateSolver`.

  Objects of this class contain both solutions and problem definition of a
  univariate OT problem.

  Args:
    prob: OT problem between 2 weighted ``[n, d]`` and ``[m, d]`` point clouds.
    ot_costs: ``[d,]`` optimal transport cost values, computed independently
      along each of the ``d`` slices.
    paired_indices: ``None`` if no transport was computed / recorded (e.g. when
      using quantiles or subsampling approximations). Otherwise, output a tensor
      of shape ``[d, 2, m+n]``, of ``m+n`` pairs of indices, for which the
      optimal transport assigns mass, on each slice of the ``d`` slices
      described in the dataset. Namely, for each index ``0<=k<m+n``, ``0<=s<d``,
      if one has ``i:=paired_indices[s,0,k]`` and ``j:=paired_indices[s,1,k]``,
      then point ``i`` in the first point cloud sends mass to point ``j`` in the
      second, in slice ``s``.
    mass_paired_indices: ``[d, n+m]`` array of weights. Using notation above, if
      ``0<=k<n+m``, and ``0<=s<d``  then writing ``i:=paired_indices[s,0,k]``
      and ``j=paired_indices[s,1,k]``, point ``i`` sends
      ``mass_paired_indices[s,k]`` to point ``j``.
    dual_a: ``[n,]`` array of dual values
    dual_b: ``[m,]`` array of dual values
  """
  prob: linear_problem.LinearProblem
  ot_costs: float
  paired_indices: Optional[jnp.ndarray] = None
  mass_paired_indices: Optional[jnp.ndarray] = None
  dual_a: Optional[jnp.ndarray] = None
  dual_b: Optional[jnp.ndarray] = None

  @property
  def transport_matrices(self) -> jnp.ndarray:
    """Outputs a ``[d, n, m]`` tensor of all ``[n, m]`` transport matrices.

    This tensor will be extremely sparse, since it will have at most ``d(n+m)``
    non-zero values, out of ``dnm`` total entries.
    """
    assert self.paired_indices is not None, \
      "[d, n, m] tensor of transports cannot be computed, likely because an" \
      " approximate method was used (using either subsampling or quantiles)."

    n, m = self.prob.geom.shape
    if self.prob.is_equal_size and self.prob.is_uniform:
      transport_matrices_from_indices = jax.vmap(
          lambda idx, idy: jnp.eye(n)[idx, :][:, idy].T, in_axes=[0, 0]
      )
      return transport_matrices_from_indices(
          self.paired_indices[:, 0, :], self.paired_indices[:, 1, :]
      )

    # raveled indexing of entries.
    indices = self.paired_indices[:, 0] * m + self.paired_indices[:, 1]
    # segment sum is needed to collect several contributions
    return jax.vmap(
        lambda idx, mass: jax.ops.segment_sum(
            mass, idx, indices_are_sorted=True, num_segments=n * m
        ).reshape(n, m),
        in_axes=[0, 0]
    )(indices, self.mass_paired_indices)

  @property
  def mean_transport_matrix(self) -> jnp.ndarray:
    """Return the mean transport matrix, averaged over slices."""
    return jnp.mean(self.transport_matrices, axis=0)


@jax.tree_util.register_pytree_node_class
class UnivariateSolver:
  r"""Univariate solver to compute 1D OT distance over slices of data.

  Computes 1-Dimensional optimal transport distance between two $d$-dimensional
  point clouds. The total distance is the sum of univariate Wasserstein
  distances on the $d$ slices of data: given two weighted point-clouds, stored
  as ``[n, d]`` and ``[m, d]`` in a
  :class:`~ott.problems.linear.linear_problem.LinearProblem` object, with
  respective weights ``a`` and ``b``, the solver
  computes ``d`` OT distances between each of these ``[n, 1]`` and ``[m, 1]``
  slices. The distance is computed using the analytical formula by default,
  which involves sorting each of the slices independently. The optimal transport
  matrices are also outputted when possible (described in sparse form, i.e.
  pairs of indices and mass transferred between those indices).

  When weights ``a`` and ``b`` are uniform, and ``n=m``, the computation only
  involves comparing sorted entries per slice, and ``d`` assignments are given.

  The user may also supply a ``num_subsamples`` parameter to extract as many
  points from the original point cloud, sampled with probability masses ``a``
  and ``b``. This then simply applied the method above to the subsamples, to
  output ``d`` costs, but assignments are not provided.

  When the problem is not uniform or not of equal size, the method defaults to
  an inversion of the CDF, and outputs both costs and transport matrix in sparse
  form.

  When a ``quantiles`` argument is passed, either specifying explicit quantiles
  or a grid of quantiles, the distance is evaluated by comparing the quantiles
  of the two point clouds on each slice. The OT costs are returned but
  assignments are not provided.

  Args:
    num_subsamples: Option to reduce the size of inputs by doing random
      subsampling, taken into account marginal probabilities.
    quantiles: When a vector or a number of quantiles is passed, the distance
      is computed by evaluating the cost function on the sectional (one for each
      dimension) quantiles of the two point cloud distributions described in the
      problem.
  """

  def __init__(
      self,
      num_subsamples: Optional[int] = None,
      quantiles: Optional[Union[int, jnp.ndarray]] = None,
  ):
    self._quantiles = quantiles
    self.num_subsamples = num_subsamples

  @property
  def quantiles(self) -> Optional[jnp.ndarray]:
    """Quantiles' values used to evaluate OT cost."""
    if self._quantiles is None:
      return None
    if isinstance(self._quantiles, int):
      return jnp.linspace(0.0, 1.0, self._quantiles)
    return self._quantiles

  @property
  def num_quantiles(self) -> int:
    """Number of quantiles used to evaluate OT cost."""
    return 0 if self.quantiles is None else self.quantiles.shape[0]

  def __call__(
      self,
      prob: linear_problem.LinearProblem,
      return_transport: bool = True,
      return_dual_vectors: bool = True,
      rng: Optional[jax.Array] = None,
  ) -> UnivariateOutput:
    """Computes Univariate Distance between the ``d`` dimensional slices.

    Args:
      prob: Problem with a :attr:`~ott.problems.linear.LinearProblem.geom`
        attribute, the two point clouds ``x`` and ``y``
        (of respective sizes ``[n, d]`` and ``[m, d]``) and a ground
        `TI cost <ott.geometry.costs.TICost>` between two scalars.
        The ``[n,]`` and ``[m,]`` size probability weights vectors are stored
        in attributes `:attr:`~ott.problems.linear.LinearProblem.a` and
        :attr:`~ott.problems.linear.LinearProblem.b`.
      return_transport: Whether to return pairs of matched indices used to
        compute optimal transport matrices.
      return_dual_vectors: Whether to return pairs of dual vectors
      rng: Used for random downsampling, if specified in the solver.

    Returns:
      An output object, that computes ``d`` OT costs, in addition to, possibly,
      paired lists of indices and their corresponding masses, on each of the
      ``d`` dimensional slices of the input.
    """
    geom = prob.geom
    assert isinstance(geom, pointcloud.PointCloud), \
      "Geometry object in problem must be a PointCloud."
    assert isinstance(geom.cost_fn, costs.TICost), \
      "Geometry's cost must be translation invariant."

    rng = utils.default_prng_key(rng)

    if self.num_subsamples:
      x, y = self._subsample(prob, rng)
      is_uniform_same_size = True
    else:
      # check if problem has the property uniform / same number of points
      x, y = geom.x, geom.y
      is_uniform_same_size = prob.is_uniform and prob.is_equal_size

    if self.quantiles is not None:
      assert prob.is_uniform, \
        "The 'quantiles' method can only be used with uniform marginals."
      out = _quant_dist(x, y, geom.cost_fn, self.quantiles, self.num_quantiles)
    elif is_uniform_same_size:
      return_transport = return_transport and not self.num_subsamples
      out = uniform_distance(x, y, geom.cost_fn, return_transport)
    else:
      fn = jax.vmap(
          quantile_distance, in_axes=[1, 1, None, None, None, None, None]
      )
      out = fn(
          x, y, geom.cost_fn, prob.a, prob.b, return_transport,
          return_dual_vectors
      )

    return UnivariateOutput(prob, *out)

  def _subsample(self, prob: linear_problem.LinearProblem,
                 rng: jax.Array) -> Tuple[jnp.ndarray, jnp.ndarray]:
    n, m = prob.geom.shape
    x, y = prob.geom.x, prob.geom.y

    if prob.is_uniform:
      x = x[jnp.linspace(0, n, num=self.num_subsamples).astype(int), :]
      y = y[jnp.linspace(0, m, num=self.num_subsamples).astype(int), :]
      return x, y

    rng1, rng2 = jax.random.split(rng, 2)
    x = jax.random.choice(rng1, x, (self.num_subsamples,), p=prob.a, axis=0)
    y = jax.random.choice(rng2, y, (self.num_subsamples,), p=prob.b, axis=0)
    return x, y

  def tree_flatten(self):  # noqa: D102
    return None, (self.num_subsamples, self._quantiles)

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    return cls(*aux_data)


def uniform_distance(
    x: jnp.ndarray,
    y: jnp.ndarray,
    cost_fn: costs.TICost,
    return_transport: bool = True
) -> Distance_t:
  """Distance between two equal-size families of uniformly weighted values x/y.

  Args:
    x: Vector ``[n,]`` of real values.
    y: Vector ``[n,]`` of real values.
    cost_fn: Translation invariant cost function, i.e. ``c(x, y) = h(x - y)``.
    return_transport: whether to return mapped pairs.

  Returns:
    optimal transport cost, a list of ``n+m`` paired indices, and their
    corresponding transport mass. Note that said mass can be null in some
    entries, but sums to 1.0
  """
  n = x.shape[0]
  i_x, i_y = jnp.argsort(x, axis=0), jnp.argsort(y, axis=0)
  x = jnp.take_along_axis(x, i_x, axis=0)
  y = jnp.take_along_axis(y, i_y, axis=0)
  ot_costs = jax.vmap(cost_fn.h, in_axes=[0])(x.T - y.T) / n

  if return_transport:
    paired_indices = jnp.stack([i_x, i_y]).transpose([2, 0, 1])
    mass_paired_indices = jnp.ones((n,)) / n
    return ot_costs, paired_indices, mass_paired_indices

  return ot_costs, None, None, None, None


def quantile_distance(
    x: jnp.ndarray,
    y: jnp.ndarray,
    cost_fn: costs.TICost,
    a: jnp.ndarray,
    b: jnp.ndarray,
    return_transport: bool = True,
    return_dual_vectors: bool = True,
) -> Distance_t:
  """Computes distance between quantile functions of distributions (a,x)/(b,y).

  Args:
    x: Vector ``[n,]`` of real values.
    y: Vector ``[m,]`` of real values.
    cost_fn: Translation invariant cost function, i.e. ``c(x, y) = h(x - y)``.
    a: Vector ``[n,]`` of non-negative weights summing to 1.
    b: Vector ``[m,]`` of non-negative weights summing to 1.
    return_transport: whether to return mapped pairs.
    return_dual_vectors: whether to return dual vectors. when set to ``True``,
      will turn ``return_transport`` to ``True`` regardless of the user choice.

  Returns:
    optimal transport cost. Optionally, a list of ``n + m`` paired indices, and
    their corresponding transport mass. Note that said mass can be null in some
    entries, but sums to 1.0. Optionally, two dual vectors corresponding to that
    transport.

  Notes:
    Inspired by :func:`~scipy.stats.wasserstein_distance`,
    but can be used with other costs, not just :math:`c(x, y) = |x - y|`.
  """
  x, i_x = mu.sort_and_argsort(x, argsort=True)
  y, i_y = mu.sort_and_argsort(y, argsort=True)

  all_values = jnp.concatenate([x, y])
  all_values_sorted, all_values_sorter = mu.sort_and_argsort(
      all_values, argsort=True
  )

  x_pdf = jnp.concatenate([a[i_x], jnp.zeros_like(b)])[all_values_sorter]
  y_pdf = jnp.concatenate([jnp.zeros_like(a), b[i_y]])[all_values_sorter]

  x_cdf = jnp.cumsum(x_pdf)
  y_cdf = jnp.cumsum(y_pdf)

  x_y_cdfs = jnp.concatenate([x_cdf, y_cdf])
  quantile_levels, _ = mu.sort_and_argsort(x_y_cdfs, argsort=False)

  i_x_cdf_inv = jnp.searchsorted(x_cdf, quantile_levels)
  x_cdf_inv = all_values_sorted[i_x_cdf_inv]
  i_y_cdf_inv = jnp.searchsorted(y_cdf, quantile_levels)
  y_cdf_inv = all_values_sorted[i_y_cdf_inv]

  diff_q = jnp.diff(quantile_levels)
  successive_costs = jax.vmap(cost_fn.h)(
      x_cdf_inv[1:, None] - y_cdf_inv[1:, None]
  )
  cost = jnp.sum(successive_costs * diff_q)
  paired_indices, mass_paired_indices, dual_a, dual_b = [
      None,
  ] * 4

  if return_transport or return_dual_vectors:
    n = x.shape[0]

    i_in_sorted_x_of_quantile = all_values_sorter[i_x_cdf_inv] % n
    i_in_sorted_y_of_quantile = all_values_sorter[i_y_cdf_inv] - n

    orig_i = i_x[i_in_sorted_x_of_quantile][1:]
    orig_j = i_y[i_in_sorted_y_of_quantile][1:]
    paired_indices, mass_paired_indices = jnp.stack([orig_i, orig_j]), diff_q

  if return_dual_vectors:
    m = y.shape[0]
    # vector of costs masked by mass transfer. only select active constraints
    support_cost = (diff_q > 0) * successive_costs
    # sort and select top n+m-1 to grab non-zero in jit-friendly manner
    idx_cost = jnp.argsort(support_cost)
    inv_idx = idx_cost[-n - m + 1:]
    new_cost = successive_costs[inv_idx]
    # select indices corresponding to active constraints
    i_orig_x, i_orig_y = orig_i[inv_idx], orig_j[inv_idx]

    def kkt(dual_ab, ridge_kernel=1e-6):
      """Eq. 3.6 in :cite:`peyre:19`, with centering constraint on dual_a."""
      dual_a, dual_b = dual_ab[:n], dual_ab[n:]
      return jnp.concatenate((
          jnp.array(jnp.sum(dual_a)).reshape((1,)),
          dual_a[i_orig_x] + dual_b[i_orig_y]
      ))

    z = jnp.concatenate((jnp.zeros((1,)), new_cost))
    operator = lx.FunctionLinearOperator(kkt, z)
    solver = lx.NormalCG(rtol=1e-6, atol=1e-6)
    sol = lx.linear_solve(operator, z, solver).value
    # split again solution into 2 dual variables
    dual_a = sol[:n]
    dual_b = sol[n:]

  return cost, paired_indices, mass_paired_indices, dual_a, dual_b


def _quant_dist(
    x: jnp.ndarray, y: jnp.ndarray, cost_fn: costs.TICost, q: jnp.ndarray,
    n_q: int
) -> Tuple[jnp.ndarray, None, None]:
  x_q = jnp.quantile(x, q, axis=0)
  y_q = jnp.quantile(y, q, axis=0)
  ot_costs = jax.vmap(cost_fn.pairwise, in_axes=[1, 1])(x_q, y_q)

  return ot_costs / n_q, None, None, None, None


@jax.tree_util.register_pytree_node_class
class GSUnivariateOutput(NamedTuple):  # noqa: D101
  """Output of the :class:`~ott.solvers.linear.GSUnivariateSolver`.

  Objects of this class contain both solutions and problem definition of a
  univariate OT problem.

  Args:
    prob: OT problem between 2 weighted ``[n, d]`` and ``[m, d]`` point clouds.
    P: ``[n,m]`` array optimal coupling matrix
    dual_a: ``[n,]`` array of dual values
    dual_b: ``[m,]`` array of dual values
  """
  prob: linear_problem.LinearProblem
  dual_a: jnp.ndarray
  dual_b: jnp.ndarray

  def tree_flatten(self):  # noqa: D102
    return None, (self.num_subsamples, self._quantiles)

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    return cls(*aux_data)

def gauss_seidel_1Dsolver(x: jnp.ndarray, 
                          y: jnp.ndarray, 
                          a: jnp.ndarray, 
                          b: jnp.ndarray, 
                          cost_fn: costs.TICost,):
    """Computes Univariate Distance between 1D point clouds.

    Args:
      x:  dual_a: ``[n,1]`` point cloud x
      y:  dual_a: ``[m,1]`` point cloud y
      a:  dual_a: ``[m]`` probability weights for point cloud x
      b:  dual_a: ``[m]`` probability weights for point cloud y
      cost_fn: Transport cost function, i.e. ``c: \mathbb{R} \times \mathbb{R} \rightarrow \mathbb{R} `.

    Returns:
        P: ``[n,m]`` array optimal coupling matrix
        dual_a: ``[n,]`` array of dual values
        dual_b: ``[m,]`` array of dual values
    """

    # sort entries
    x, i_x = mu.sort_and_argsort(x, argsort=True)
    y, i_y = mu.sort_and_argsort(y, argsort=True)
    a = a[i_x]
    b = b[i_y]

    # compute cost matrix
    cost_matrix = cost_fn.pairwise(x, y)

    n, m = len(a), len(b)

    # cumulative idx
    q = m+n-1
    I = J = jnp.zeros(q, dtype=int)

    # transport matrix
    P = jnp.zeros((n,m))
                  
    # init duals
    dual_a, dual_b = jnp.zeros(n), jnp.zeros(m)
    dual_b = dual_b.at[0].set(cost_matrix[0,0])

    # helper functions
    def dual_a_update(I, J, dual_a, dual_b, i, j, k):
      I = I.at[k+1].set(i+1)
      J = J.at[k+1].set(j)
      dual_a = dual_a.at[i+1].set(cost_matrix[i+1,j] - dual_b[j])
      return I, J, dual_a, dual_b

    def dual_b_update(I, J, dual_a, dual_b, i, j, k):
      I = I.at[k+1].set(i)
      J = J.at[k+1].set(j+1)
      dual_b = dual_b.at[j+1].set(cost_matrix[i,j+1] - dual_a[i])
      return I, J, dual_a, dual_b

    def body_fun(k, val):
      (P, I, J, a, b, dual_a, dual_b) = val
      i, j  = I[k], J[k]
      I, J, dual_a, dual_b = jax.lax.cond(a[i]<b[j],
                                dual_a_update,
                                dual_b_update,
                                *(I, J, dual_a, dual_b, i, j, k))

      min_ab = jnp.minimum(a[i], b[j])
      P = P.at[i,j].set(min_ab)
      a = a.at[i].set(a[i] - min_ab)
      b = b.at[j].set(b[j] - min_ab)

      return P, I, J, a, b, dual_a, dual_b

    # main
    init_val = (P, I, J, a, b, dual_a, dual_b)
    P, I, J, a, b, dual_a, dual_b = jax.lax.fori_loop(0, q-1, body_fun, init_val)

    p_final = jnp.maximum(a[-1], b[-1])
    i, j  = I[-1], J[-1]
    P = P.at[i,j].set(p_final)

    return P, dual_a, dual_b


@jax.tree_util.register_pytree_node_class
class GSUnivariateSolver:
  r"""Gauss–Seidel solver to compute 1D OT solution.

  Computes 1-Dimensional optimal transport distance between two $1$-dimensional
  point clouds using algorithm 1 in https://arxiv.org/pdf/2201.00730.pdf
  """

  def __call__(
      self,
      prob: linear_problem.LinearProblem,
  ) -> GSUnivariateOutput:
    """Computes Univariate Distance between 1D point clouds.

    Args:
      prob: Problem with a :attr:`~ott.problems.linear.LinearProblem.geom`
        attribute, the two point clouds ``x`` and ``y``
        (of respective sizes ``[n, d]`` and ``[m, d]``) and a ground
        `TI cost <ott.geometry.costs.TICost>` between two scalars.
        The ``[n,]`` and ``[m,]`` size probability weights vectors are stored
        in attributes `:attr:`~ott.problems.linear.LinearProblem.a` and
        :attr:`~ott.problems.linear.LinearProblem.b`.

    Returns:
      An output object, that holds the optimal transport plan as well as dual potentials 
    """
    geom = prob.geom
    assert isinstance(geom, pointcloud.PointCloud), \
      "Geometry object in problem must be a PointCloud."
    assert isinstance(geom.cost_fn, costs.TICost), \
      "Geometry's cost must be translation invariant."
    assert geom.x.shape[-1] == 1 and geom.y.shape[-1] == 1, \
    "Univariate solver must be applied to point clouds of dimension 1."
    
    P, dual_a, dual_b = gauss_seidel_1Dsolver(x=geom.x, 
                                              y=geom.y, 
                                              a=prob.a, 
                                              b=prob.b, 
                                              cost_fn = geom.cost_fn)
  
    return GSUnivariateOutput(P=P, dual_a=dual_a, dual_b=dual_b)

  def tree_flatten(self):  # noqa: D102
    return None, (self.num_subsamples, self._quantiles)

  @classmethod
  def tree_unflatten(cls, aux_data, children):  # noqa: D102
    del children
    return cls(*aux_data)


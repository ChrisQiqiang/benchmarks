# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Utilities for VariableMgr."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections as pycoll
import operator

import numpy as np
import tensorflow.compat.v1 as tf

# pylint: disable=g-direct-tensorflow-import
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import data_flow_ops
from tensorflow.python.ops import math_ops


PS_SHADOW_VAR_PREFIX = 'ps_var'

AutoLossScaleParams = pycoll.namedtuple(
    'AutoLossScaleParams',
    [
        # If true, enable automatic loss scaling.
        'enable_auto_loss_scale',
        # The value to scale the loss before computing gradients.
        'loss_scale',
        # Number of normal steps with the current `loss_scale`.
        'loss_scale_normal_steps',
        # Increase loss scale every n steps.
        'inc_loss_scale_every_n',
        # If true, the current worker is chief. The current implementation
        # relies on the chief to update loss_scale value, but in future, we
        # might change this to ask the parameter server to update loss_scales
        # for better performance.
        # TODO(tanmingxing): remove this if loss_scale is updated in ps.
        'is_chief',
    ])


def get_loss_scale_update_op(loss_scale, loss_scale_normal_steps,
                             inc_loss_scale_every_n):
  """Returns the update op for loss scaling variables.

  We maintain the counter `loss_scale_normal_steps` to count the number of steps
  we have been using the current `loss_scale`. In most cases, this function
  increments `loss_scale_normal_steps`. However, if `loss_scale_normal_steps` is
  greater than the threshold `inc_loss_scale_every_n`, we double `loss_scale`
  and reset `loss_scale_normal_steps` to zero.

  This op is only called if the gradients don't have any infs or nans. Instead,
  if infs or nans occur in the gradients, we immeditately halve `loss_scale` and
  reset `loss_scale_normal_steps` to zero.

  Args:
    loss_scale: a tf.Variable represneting the loss_scale value.
    loss_scale_normal_steps: a tf.Variable representing the number of training
      steps that have run since the loss_scale last changed.
    inc_loss_scale_every_n: a Python integer threshold. `loss_scale` is
      increased every `inc_loss_scale_every_n` steps, unless the gradients have
      infs or nans.

  Returns:
    An op for updating `loss_scale` and `loss_scale_normal_steps`.
  """

  def increment_loss_scale_normal_steps_func():
    return tf.group(loss_scale_normal_steps.assign_add(1))

  def increase_loss_scale_func():
    return tf.group(
        tf.assign(loss_scale_normal_steps, 0),
        tf.assign(loss_scale, loss_scale * 2))

  # true_fn and false_fn must have the same type.
  return tf.cond(loss_scale_normal_steps < inc_loss_scale_every_n,
                 increment_loss_scale_normal_steps_func,
                 increase_loss_scale_func)


def append_gradients_with_loss_scale(training_ops, get_apply_gradients_ops_func,
                                     loss_scale_params, grad_has_inf_nan):
  """Selectively appends gradients update ops with loss scaling.

  Args:
    training_ops: a list of training ops to be executed.
    get_apply_gradients_ops_func: a function that returns a list of ops for
      applying gradients. Here, we must pass a function instead of the actual
      list of ops; otherwise, those ops would be executed unconditionally due to
      the semantics of tf.cond.
    loss_scale_params: An AutoLossScaleParams tuple.
    grad_has_inf_nan: Boolean tensor indicating whether the gradients have infs
      or nans.
  """
  is_chief = loss_scale_params.is_chief
  loss_scale = loss_scale_params.loss_scale
  loss_scale_normal_steps = loss_scale_params.loss_scale_normal_steps
  inc_loss_scale_every_n = loss_scale_params.inc_loss_scale_every_n
  enable_auto_loss_scale = loss_scale_params.enable_auto_loss_scale

  if loss_scale is None or not enable_auto_loss_scale or not is_chief:
    training_ops.extend(get_apply_gradients_ops_func())
  else:
    # If nans/infs occurred, loss_scaleskip applying gradients and instead update
    # loss_scale (halve loss_scale and reset loss_scale_normal_steps to zero).
    def update_op_if_nan_or_inf():
      """Update loss_scale and discard gradients if nans/infs occurred."""
      return tf.group(
          tf.assign(loss_scale, loss_scale / 2.),
          tf.assign(loss_scale_normal_steps, 0))

    # Otherwise, apply gradients, and update loss_scale and
    # loss_scale_normal_steps.
    def update_op_if_no_nan_or_inf():
      """Apply gradients, and update loss scaling."""
      return tf.group(
          get_loss_scale_update_op(loss_scale, loss_scale_normal_steps,
                                   inc_loss_scale_every_n),
          *get_apply_gradients_ops_func())

    # TODO(tanmingxing): Add support for independent and distributed all_reduce.
    assert grad_has_inf_nan is not None
    update_op = tf.cond(
        grad_has_inf_nan,
        update_op_if_nan_or_inf,
        update_op_if_no_nan_or_inf,
        name='cond_if_grad_has_inf_nan'
    )
    training_ops.append(update_op)


# To be used with custom_getter on tf.get_variable.
class OverrideCachingDevice(object):
  """Variable getter which caches variables on the least loaded device.

  Variables smaller than a certain threshold are cached on a single specific
  device, as specified in the constructor. All other variables are load balanced
  across a pool of devices, by caching each variable on the least loaded device.

  Note that variable creation only happen when building the model graph on the
  first device (see how it sets the 'reuse' parameter in
  VariableMgr.*.create_outer_variable_scope()). That means, for all other
  devices, the variable scope will reuse the variables created before, which
  requires that we set the caching_device correctly as otherwise it may not be
  able to find the previously created variable and will create a new one. This
  requires when building the model graph on different devices, variables with
  the same name should have same size.

  TODO(laigd): consider adding tests or verification logic to enforce this, or
  refactor it.
  """

  def __init__(self, devices, device_for_small_variables,
               small_variable_size_threshold):
    self.devices = devices
    self.sizes = [0] * len(self.devices)
    self.device_for_small_variables = device_for_small_variables
    self.small_variable_size_threshold = small_variable_size_threshold

  def __call__(self, getter, *args, **kwargs):
    size = tf.TensorShape(kwargs['shape']).num_elements()
    if size < self.small_variable_size_threshold:
      device_name = self.device_for_small_variables
    else:
      device_index, _ = min(enumerate(self.sizes), key=operator.itemgetter(1))
      device_name = self.devices[device_index]
      self.sizes[device_index] += size

    kwargs['caching_device'] = device_name
    var = getter(*args, **kwargs)
    return var


# To be used with custom_getter on tf.get_variable. Ensures the created variable
# is in LOCAL_VARIABLES and not GLOBAL_VARIBLES collection.
class OverrideToLocalVariableIfNotPsVar(object):

  # args and kwargs come from the custom_getter interface for Tensorflow
  # variables, and matches tf.get_variable's signature, with the addition of
  # 'getter' at the beginning.
  def __call__(self, getter, name, *args, **kwargs):
    if name.startswith(PS_SHADOW_VAR_PREFIX):
      return getter(*args, **kwargs)

    if 'collections' in kwargs:
      collections = kwargs['collections']
    if not collections:
      collections = [tf.GraphKeys.GLOBAL_VARIABLES]
    else:
      collections = collections[:]
    collections.remove(tf.GraphKeys.GLOBAL_VARIABLES)
    collections.append(tf.GraphKeys.LOCAL_VARIABLES)
    kwargs['collections'] = list(collections)
    return getter(name, *args, **kwargs)


class ParamServerDeviceSetter(object):
  """Helper class to assign variables on the least loaded ps-device."""

  def __init__(self, worker_device, ps_devices):
    """Initializer for ParamServerDevicSetter.

    Args:
      worker_device: the device to use for computer ops.
      ps_devices: a list of device to use for Variable ops. Each variable is
      assigned to the least loaded device.
    """
    self.ps_devices = ps_devices
    self.worker_device = worker_device
    self.ps_sizes = [0] * len(self.ps_devices)

  def __call__(self, op):
    if op.device:
      return op.device
    if op.type not in ['Variable', 'VariableV2']:
      return self.worker_device

    device_index, _ = min(enumerate(self.ps_sizes), key=operator.itemgetter(1))
    device_name = self.ps_devices[device_index]
    var_size = op.outputs[0].get_shape().num_elements()
    self.ps_sizes[device_index] += var_size

    return device_name


class StagedModelVariable(object):
  """Staging variable wrapper that decouples reads and updates.

  This class represents a variable through a staging buffer. Reads from this
  variable directly gets from the staging buffer. Updates are stacked into
  another staging buffer, and will be processed later.
  """

  def __init__(self, real_var, var_stage_get, variable_mgr):
    """Initializer for the model variables through a staging buffer.

    Args:
      real_var: the underlying real variable.
      var_stage_get: the read op from the staging buffer.
      variable_mgr: the parent variable-manager.
    """
    self.real_var = real_var
    self.var_stage_get = var_stage_get
    self.variable_mgr = variable_mgr

  def _value(self):
    """The read access of this variable. The content from the staging buffer."""
    return self.var_stage_get

  def _ref(self):
    """Return the underlying variable ref, required by tf.colocate_with."""
    return self.real_var._ref()  # pylint: disable=protected-access

  def read_value(self):
    """Mimics tf.Variable.read_value()."""
    return tf.identity(self.var_stage_get, name='read')

  @property
  def dtype(self):
    """Return the non-reference dtype."""
    return self.var_stage_get.dtype

  def assign_sub(self, delta, name=None, read_value=True):
    """Mimic the updates to the variable.

    Args:
      delta: is pushed into a staging buffer and will be pumped later.
      name: currently ignored; names of ops and the StagingArea are
            computed without using this pass name.
      read_value: if True, will return something which evaluates to the new
              value of the variable; if False will return the assign op.
    Returns:
      The actual updates. The colocation constraint will be reapplied.
    """
    # This parameter is ignored: the StagingArea only supports setting
    # the shared name, not the names of individual ops it uses.
    del name

    # colocate_with(None, True) clears the colocation constraints.
    # Push the delta into a staging buffer.
    with ops.colocate_with(None, True), tf.device(self.var_stage_get.device):
      delta_staging_area = data_flow_ops.StagingArea(
          [self.var_stage_get.dtype], shapes=[self.var_stage_get.shape])
      delta_put_op = delta_staging_area.put([delta])
      self.variable_mgr.staging_delta_ops.append(delta_put_op)
      delta_get_op = delta_staging_area.get()[0]
    # Return the actual updates. The colocation constraint will be reapplied.
    return self.real_var.assign_sub(delta_get_op, read_value=read_value)

  @staticmethod
  # pylint: disable=bad-staticmethod-argument,invalid-name
  def _TensorConversionFunction(self, dtype=None, name=None, as_ref=False):
    """Utility function for converting a StagedModelVariable to a Tensor."""
    del dtype, name  # unused: this function returns the cached ref or value.
    if as_ref:
      return self._ref()
    else:
      return self._value()


ops.register_tensor_conversion_function(
    StagedModelVariable, StagedModelVariable._TensorConversionFunction)  # pylint: disable=protected-access


class StagedVariableGetter(object):
  """A variable getter through staging buffers on devices.

  Instead of a caching device, this getter tracks where the variable is used.
  And on each device, it goes through a staging buffer.
  """

  def __init__(self, device_num, devices, cpu_device, variable_mgr):
    """Initializer for StagedVariableGetter.

    Args:
      device_num: the current device index.
      devices: a list of all the devices to build towers.
      cpu_device: a cpu_device for this replica. If None, no cpu-caching is
          done.
      variable_mgr: the parent variable manager.
    """
    self.device_num = device_num
    self.devices = devices
    self.cpu_device = cpu_device
    self.variable_mgr = variable_mgr

  def __call__(self, getter, name, *args, **kwargs):
    staging_ops = self.variable_mgr.staging_vars_on_devices[self.device_num]
    if name in staging_ops:
      put_op, get_op = staging_ops[name]
      return get_op
    real_var = getter(name, *args, **kwargs)
    shape = kwargs['shape']
    dtype = kwargs['dtype']
    trainable = kwargs['trainable']
    if self.cpu_device:
      with tf.device(self.cpu_device):
        # This helps copying the weights from the parameter to this server only
        # once.
        if name in self.variable_mgr.staged_vars_on_cpu:
          cpu_var = self.variable_mgr.staged_vars_on_cpu[name]
        else:
          cpu_var = tf.identity(real_var)
          self.variable_mgr.staged_vars_on_cpu[name] = cpu_var
      var_to_stage = cpu_var
    else:
      var_to_stage = tf.identity(real_var)  # de-reference the variable.

    with tf.device(self.devices[self.device_num]):
      staging_area = data_flow_ops.StagingArea([dtype], shapes=[shape])
      put_op = staging_area.put([var_to_stage])
      get_op = staging_area.get()[0]
      staging_ops[name] = (put_op, get_op)
    if trainable:
      # For trainable variables, they are managed separatedly through
      # apply_gradients.
      return get_op
    else:
      # For other shadow variables, the access is decoupled through a wrapper
      # class.
      return StagedModelVariable(real_var, get_op, self.variable_mgr)

  def trainable_variables_on_device(self, rel_device_num, abs_device_num,
                                    writable):
    """Return the set of trainable variables on the specified device.

    Args:
      rel_device_num: local worker device index.
      abs_device_num: global graph device index.
      writable: whether the returned variables is writable or read-only.

    Returns:
      Return the set of trainable variables on the specified device.
    """
    del abs_device_num
    params_refs = tf.trainable_variables()
    if writable:
      return params_refs
    params = []
    for param in params_refs:
      var_name = param.name.split(':')[0]
      # print(var_name)
      # if 'global image' in var_name:
        # print("guolv global image success. {}\n\n\n".format(var_name))
        # continue
      _, var_get_op = self.variable_mgr.staging_vars_on_devices[rel_device_num][
          var_name]
      params.append(var_get_op)
    return params


def aggregate_gradients_using_copy_with_device_selection(
    benchmark_cnn, tower_grads, use_mean, check_inf_nan):
  """Aggregate gradients, controlling device for the aggregation.

  Args:
    benchmark_cnn: benchmark_cnn class.
    tower_grads: List of lists of (gradient, variable) tuples. The outer list
      is over towers. The inner list is over individual gradients.
    use_mean: if True, mean is taken, else sum of gradients is taken.
    check_inf_nan: If true, check grads for nans and infs.

  Returns:
    The tuple ([(average_gradient, variable),], has_nan_or_inf) where the
      gradient has been averaged across all towers. The variable is chosen from
      the first tower. The has_nan_or_inf indicates the grads has nan or inf.
  """
  if benchmark_cnn.local_parameter_device_flag == 'gpu':
    avail_devices = benchmark_cnn.raw_devices
  else:
    avail_devices = [benchmark_cnn.param_server_device]
  agg_grads = []
  has_nan_or_inf_list = []
  for i, single_grads in enumerate(zip(*tower_grads)):
    with tf.device(avail_devices[i % len(avail_devices)]):
      grad_and_var, has_nan_or_inf = aggregate_single_gradient_using_copy(
          single_grads, use_mean, check_inf_nan)
      agg_grads.append(grad_and_var)
      has_nan_or_inf_list.append(has_nan_or_inf)
  if check_inf_nan:
    return agg_grads, tf.reduce_any(has_nan_or_inf_list)
  else:
    return agg_grads, None


def aggregate_gradients_using_copy_with_variable_colocation(
    tower_grads, use_mean, check_inf_nan):
  """Aggregate gradients, colocating computation with the gradient's variable.

  Args:
    tower_grads: List of lists of (gradient, variable) tuples. The outer list
      is over towers. The inner list is over individual gradients. All variables
      of the same gradient across towers must be the same (that is,
      tower_grads[x][a][1] == tower_grads[y][a][1] for all indices x, y, and a)
    use_mean: if True, mean is taken, else sum of gradients is taken.
    check_inf_nan: If true, check grads for nans and infs.

  Returns:
    The tuple ([(average_gradient, variable),], has_nan_or_inf) where the
      gradient has been averaged across all towers. The variable is chosen from
      the first tower. The has_nan_or_inf indicates the grads has nan or inf.
  """
  agg_grads = []
  has_nan_or_inf_list = []
  for single_grads in zip(*tower_grads):
    # Note that each single_grads looks like the following:
    #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
    var = single_grads[0][1]

    for _, v in single_grads:
      assert v == var

    with tf.device(var.device):
      # print("aggregate gradients of var {} in device {}".format(var, var.device))
      grad_and_var, has_nan_or_inf = aggregate_single_gradient_using_copy(
          single_grads, use_mean, check_inf_nan)
      agg_grads.append(grad_and_var)
      has_nan_or_inf_list.append(has_nan_or_inf)

  if check_inf_nan:
    return agg_grads, tf.reduce_any(has_nan_or_inf_list)
  else:
    return agg_grads, None


def aggregate_gradients_using_copy(tower_grads, use_mean, check_inf_nan):
  """Calculate the average gradient for each shared variable across all towers.

  Note that this function provides a synchronization point across all towers.

  Args:
    tower_grads: List of lists of (gradient, variable) tuples. The outer list
      is over towers. The inner list is over individual gradients.
    use_mean: if True, mean is taken, else sum of gradients is taken.
    check_inf_nan: check grads for nans and infs.

  Returns:
    The tuple ([(average_gradient, variable),], has_nan_or_inf) where the
      gradient has been averaged across all towers. The variable is chosen from
      the first tower. The has_nan_or_inf indicates the grads has nan or inf.
  """
  agg_grads = []
  has_nan_or_inf_list = []
  # for i, single_grads in enumerate(zip(*tower_grads)):
  #   print("\naggregate the single gradient {} \n, {} \n, {}\n\n\n ".format(i, 
  #   single_grads[0].shape() if single_grads[0] is not None else single_grads[0],
  #   single_grads[1].shape() if single_grads[1] is not None else single_grads[1]))

  for single_grads in zip(*tower_grads):
    grad_and_var, has_nan_or_inf = aggregate_single_gradient_using_copy(
        single_grads, use_mean, check_inf_nan)
    agg_grads.append(grad_and_var)
    has_nan_or_inf_list.append(has_nan_or_inf)

  if check_inf_nan:
    return agg_grads, tf.reduce_any(has_nan_or_inf_list)
  else:
    return agg_grads, None


# The following two functions are copied from
# tensorflow/python/eager/backprop.py. We do not directly use them as they are
# not exported and subject to change at any time.
def flatten_nested_indexed_slices(grad):
  assert isinstance(grad, ops.IndexedSlices)
  if isinstance(grad.values, ops.Tensor):
    return grad
  else:
    assert isinstance(grad.values, ops.IndexedSlices)
    g = flatten_nested_indexed_slices(grad.values)
    return ops.IndexedSlices(g.values, array_ops.gather(grad.indices,
                                                        g.indices),
                             g.dense_shape)


def aggregate_indexed_slices_gradients(grads):
  """Aggregates gradients containing `IndexedSlices`s."""
  if len(grads) < 1:
    return None
  elif len(grads) == 1:
    return grads[0]
  else:
    grads = [g for g in grads if g is not None]
    # If any gradient is a `Tensor`, sum them up and return a dense tensor
    # object.
    if any(isinstance(g, ops.Tensor) for g in grads):
      return math_ops.add_n(grads)

    # The following `_as_indexed_slices_list` casts ids of IndexedSlices into
    # int64. It is to make sure the inputs of `concat` all have same the data
    # type.
    grads = math_ops._as_indexed_slices_list(grads)  # pylint: disable=protected-access

    grads = [flatten_nested_indexed_slices(x) for x in grads]
    # Form IndexedSlices out of the concatenated values and indices.
    concat_grad = ops.IndexedSlices(
        array_ops.concat([x.values for x in grads], axis=0),
        array_ops.concat([x.indices for x in grads], axis=0),
        grads[0].dense_shape)

    return concat_grad


def aggregate_single_gradient_using_copy(grad_and_vars, use_mean,
                                         check_inf_nan):
  """Calculate the average gradient for a shared variable across all towers.

  Note that this function provides a synchronization point across all towers.

  Args:
    grad_and_vars: A list or tuple of (gradient, variable) tuples. Each
      (gradient, variable) pair within the outer list represents the gradient
      of the variable calculated for a single tower, and the number of pairs
      equals the number of towers.
    use_mean: if True, mean is taken, else sum of gradients is taken.
    check_inf_nan: check grads for nans and infs.

  Returns:
    The tuple ([(average_gradient, variable),], has_nan_or_inf) where the
      gradient has been averaged across all towers. The variable is chosen from
      the first tower. The has_nan_or_inf indicates the grads has nan or inf.
  """
  grads = [g for g, _ in grad_and_vars]
  if any(isinstance(g, tf.IndexedSlices) for g in grads):
    # TODO(reedwm): All-reduce IndexedSlices more effectively.
    grad = aggregate_indexed_slices_gradients(grads)
  else:
    # for grad in grads:
    #   print("grads len {},  {} ".format(len(grads), grad))
    grad = tf.add_n(grads)

  if use_mean and len(grads) > 1:
    grad = tf.scalar_mul(1.0 / len(grads), grad)

  v = grad_and_vars[0][1]
  if check_inf_nan:
    with tf.name_scope('check_for_inf_and_nan'):
      has_nan_or_inf = tf.logical_not(tf.reduce_all(tf.is_finite(grads)))
    return (grad, v), has_nan_or_inf
  else:
    return (grad, v), None


# This class is copied from
# https://github.com/tensorflow/tensorflow/blob/590d6eef7e91a6a7392c8ffffb7b58f2e0c8bc6b/tensorflow/contrib/training/python/training/device_setter.py#L56.
# We copy it since contrib has been removed from TensorFlow.
class GreedyLoadBalancingStrategy(object):
  """Returns the least-loaded ps task for op placement.

  The load is calculated by a user-specified load function passed in at
  construction.  There are no units for load, and the load function is
  responsible for providing an internally consistent measure.

  Note that this strategy is very sensitive to the exact order in which
  ps ops (typically variables) are created, as it greedily places ops
  on the least-loaded ps at the point each op is processed.

  One reasonable heuristic is the `byte_size_load_fn`, which
  estimates load as the number of bytes that would be used to store and
  transmit the entire variable.  More advanced load functions
  could consider the difference in access patterns across ops, or trade
  off CPU-intensive ops with RAM-intensive ops with network bandwidth.

  This class is intended to be used as a `ps_strategy` in
  `tf.compat.v1.train.replica_device_setter`.
  """

  def __init__(self, num_tasks, load_fn):
    """Create a new `LoadBalancingStrategy`.

    Args:
      num_tasks: Number of ps tasks to cycle among.
      load_fn: A callable that takes an `Operation` and returns a
        numeric load value for that op.
    """
    self._num_tasks = num_tasks
    self._load_fn = load_fn
    self._ps_loads = np.zeros(num_tasks)

  def __call__(self, op):
    """Choose a ps task index for the given `Operation`.

    Args:
      op: A `Operation` to be placed on ps.

    Returns:
      The next ps task index to use for the `Operation`. Greedily
      places the op on the least-loaded ps task so far, as determined
      by the load function.
    """
    task = np.argmin(self._ps_loads)
    self._ps_loads[task] += self._load_fn(op)
    return task


# This function is copied from
# https://github.com/tensorflow/tensorflow/blob/590d6eef7e91a6a7392c8ffffb7b58f2e0c8bc6b/tensorflow/contrib/training/python/training/device_setter.py#L105.
# We copy it since contrib has been removed from TensorFlow.
def byte_size_load_fn(op):
  """Load function that computes the byte size of a single-output `Operation`.

  This is intended to be used with `"Variable"` ops, which have a single
  `Tensor` output with the contents of the variable.  However, it can also be
  used for calculating the size of any op that has a single output.

  Intended to be used with `GreedyLoadBalancingStrategy`.

  Args:
    op: An `Operation` with a single output, typically a "Variable" op.

  Returns:
    The number of bytes in the output `Tensor`.

  Raises:
    ValueError: if `op` does not have a single output, or if the shape of the
      single output is not fully-defined.
  """
  if len(op.outputs) != 1:
    raise ValueError('Op %s must have a single output' % op)
  output = op.outputs[0]
  elem_size = output.dtype.size
  shape = output.get_shape()
  if not shape.is_fully_defined():
    # Due to legacy behavior, scalar "Variable" ops have output Tensors that
    # have unknown shape when the op is created (and hence passed to this
    # load function for placement), even though the scalar shape is set
    # explicitly immediately afterward.
    shape = tensor_shape.TensorShape(op.get_attr('shape'))
  shape.assert_is_fully_defined()
  return shape.num_elements() * elem_size


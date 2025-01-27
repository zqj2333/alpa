"""
Advanced API Usage
==================

This page will cover some more advanced examples of Alpa.
"""

###########################################
# We first import libraries and create example model and train step functions.

import flax.linen as nn
import jax
import jax.numpy as jnp
import ray
import optax

import alpa
from alpa import global_config, parallelize
from alpa.device_mesh import DeviceCluster
from alpa.model.bert_model import BertConfig, FlaxBertLayer
from alpa.model.model_util import TrainState
from alpa.util import count_communication_primitives, get_ray_namespace_str

# launch the cluster
ray.init()
cluster = DeviceCluster()
global_config.devices = cluster.get_physical_mesh()

# define consts
batch_size = 64
seq_len = 512
hidden_size = 512
num_heads = 4
n_layers = 4


# Define model, train state and train step
class BertLayerModel(nn.Module):
    config: BertConfig
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        self.layers = [
            FlaxBertLayer(config=self.config, dtype=self.dtype)
            for _ in range(self.config.num_hidden_layers)
        ]

    def __call__(self, x, attention_mask):
        for i, layer in enumerate(self.layers):
            layer_outputs = layer(x, attention_mask)
            x = layer_outputs[0]
        return x


def create_train_state(rngkey, model, inputs):
    params = model.init(rngkey, *inputs)
    tx = optax.adam(learning_rate=1e-2)
    state = TrainState.create(apply_fn=model.apply,
                              params=params,
                              tx=tx,
                              dynamic_scale=None)
    return state


rngkey = jax.random.PRNGKey(0)
x = jax.random.normal(rngkey, (batch_size, seq_len, hidden_size))
y = jax.random.normal(rngkey, (batch_size, seq_len, hidden_size))
attention_mask = jnp.ones((batch_size, seq_len), dtype=jnp.float32)
batch = {'x': x, 'y': y, "attention_mask": attention_mask}
bert_config = BertConfig(hidden_size=hidden_size,
                         intermediate_size=hidden_size * 4,
                         num_attention_heads=num_heads,
                         num_hidden_layers=n_layers)
model = BertLayerModel(config=bert_config)
state = create_train_state(rngkey, model, [x, attention_mask])


def train_step(state, batch):

    def loss_func(params):
        out = state.apply_fn(params, batch["x"], batch["attention_mask"])
        loss = jnp.mean((out - batch["y"])**2)
        return loss

    grads = jax.grad(loss_func)(state.params)
    new_state = state.apply_gradients(grads=grads)
    return new_state


# define test utils
def print_hlo_communication_stats(hlo_text):
    (n_total, n_all_reduce, n_all_gather, n_reduce_scatter,
     n_all_to_all) = count_communication_primitives(hlo_text)

    print(f"#total: {n_total}, #all-reduce: {n_all_reduce}, "
          f"#all-gather: {n_all_gather}, #reduce-scatter: {n_reduce_scatter}, "
          f"#all-to-all: {n_all_to_all}")


def reset_state():
    global state
    state = create_train_state(rngkey, model, [x, attention_mask])


###########################################
# Auto-Sharding Options
# ~~~~~~~~~~~~~~~~~~~~~
#
# AutoShardingOption is designed to control the inter-operator parallelism more precisely.
#
# Control specific collective primitive
# -----------------------------------------
#
# Some primitive is not well-supported on specific platforms(e.g. may cause deadlock).
# In case of that, they should be excluded in auto-sharding's optimization space.
# We control this by some auto-sharding options.
#
# In some cases, an allreduce can be replaced by a reduce-scatter first,
# and an all-gather later. The two has the same communication, but reduce-scatter
# may readuce the peak memory.

as_option = global_config.default_autosharding_option
as_option_backup = as_option.backup()

as_option.prefer_reduce_scatter = True
executable = parallelize(train_step).get_executable(state, batch)
print_hlo_communication_stats(executable.get_hlo_text())

# create new state to avoid jit
as_option.prefer_reduce_scatter = False
state = create_train_state(rngkey, model, [x, attention_mask])
executable = parallelize(train_step).get_executable(state, batch)
print_hlo_communication_stats(executable.get_hlo_text())

as_option.restore(as_option_backup)

###########################################
# Force to use data parallel
# --------------------------
#
# Alpa can forcibly generates data parallel solution, or map a specific
# mesh dimension to the batch dimension.
#
# With force_batch_dim_to_mesh_dim, Alpa forcibly maps the given logical mesh
# dimension (0 or 1) to batch dimension inferred in auto-sharding.
# If the option's value is None, but the two dimensions of the logical mesh is
# larger than 1, Alpa still forcibly maps the first logical mesh dimension to
# batch dimension.
#
# With force_data_parallel, Alpa sets the first dimension larger than 1 to the force_batch_dim_to_mesh_dim value.

# Default mesh shape: (num_host,num_device)=(1,4)

as_option.force_batch_dim_to_mesh_dim = 0
reset_state()
executable = parallelize(train_step).get_executable(state, batch)
print_hlo_communication_stats(executable.get_hlo_text())
# The above uses model parallel

as_option.force_batch_dim_to_mesh_dim = 1
reset_state()
executable = parallelize(train_step).get_executable(state, batch)
print_hlo_communication_stats(executable.get_hlo_text())
# The above uses data parallel

as_option.restore(as_option_backup)

###########################################
# Specify inter-operator parallelism strategy
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
# We can specify inter-operator parallelism config with global_config.
# To start with, we first set parallel strategy to 3d parallel and use alpa's grad decorator:

global_config.devices.shutdown()
global_config.strategy = "pipeshard_parallel"
global_config.devices = cluster.get_virtual_physical_mesh()


def train_step(state, batch):

    def loss_func(params):
        out = state.apply_fn(params, batch["x"])
        loss = jnp.mean((out - batch["y"])**2)
        return loss

    # modify the grad decorator here
    grads = alpa.grad(loss_func)(state.params)
    new_state = state.apply_gradients(grads=grads)
    return new_state


def profile_and_pp_pipeshard_stats(executable):
    pipeshard_stats = executable.profile_all_executables()
    print("All stages' stats in form of (time, memory)")
    for mesh_idx, mesh_stats in enumerate(pipeshard_stats):
        output_str = ""
        for stat in mesh_stats.values():
            output_str += f"({stat[0]:.3f}s,{stat[1]:.2f}GB),"
        print(f"mesh {mesh_idx}:" + output_str)


###########################################
# Specify layer clustering
# ------------------------
#
# Layer cluster forms a number of JaxprEqns (atom in JAX IR) into the same layer.
# We can also manually assign layers using the pipeline marker.

from alpa import mark_pipeline, manual_layer_construction


class UnequalManualLayerBertLayerModel(nn.Module):
    config: BertConfig
    dtype: jnp.dtype = jnp.float32
    manual_pipeline_layer: bool = True

    def setup(self):
        self.layers = [
            FlaxBertLayer(config=self.config, dtype=self.dtype)
            for _ in range(self.config.num_hidden_layers)
        ]

    def __call__(self, x, attention_mask):
        for i, layer in enumerate(self.layers):
            # Add the pipeline start marker here
            if i < 2:
                mark_pipeline(name=str(i), mark_type='start')
            layer_outputs = layer(x, attention_mask)
            x = layer_outputs[0]
            # Add the pipeline end marker here
            if i == 0 or i == self.config.num_hidden_layers - 1:
                mark_pipeline(name=str(i), mark_type='end')
        return x


def train_step(state, batch):
    # Add the manual layer construction decorator here
    @manual_layer_construction(lift_markers=True)
    def loss_func(params):
        out = state.apply_fn(params, batch["x"], batch["attention_mask"])
        loss = jnp.mean((out - batch["y"])**2)
        return loss

    grads = alpa.grad(loss_func)(state.params)
    new_state = state.apply_gradients(grads=grads)
    return new_state


model = UnequalManualLayerBertLayerModel(config=bert_config)
state = create_train_state(rngkey, model, [x, attention_mask])

executable = parallelize(train_step).get_executable(state, batch)
profile_and_pp_pipeshard_stats(executable)

executable.shutdown()

###########################################
# The code above creates a model with four bert layers, then split them into
# two alpa layers. With default setting, each layer maps a pipeline stage and
# each stage use the same submesh. As we split between the first bert layer and
# the other three layers, the memory consumption of the first stage is
# approximately third of the second's.
#
# In manual layer construction, each instruction in the forward computation
# should between a pipeline start marker and its corresponding pipeline end
# marker. When using the manual pipeline marker, the loss function should be
# decorated by the manual_layer_construction mark.
#
# For simplicity, manual_layer_construction provides a lift_marker option.
# If it is turned on, the first and last pipeline marker are automatically
# moved to the first and last JaxprEqn.
#
# Specify stage construction
# --------------------------
#
# Stage construction merges layers into stages and assigns devices to each stage
# with a logical mesh shape. Here we manually give the stage construction plan
# with options in global_config.


class EqualManualLayerBertLayerModel(nn.Module):
    config: BertConfig
    dtype: jnp.dtype = jnp.float32
    manual_pipeline_layer: bool = True

    def setup(self):
        self.layers = [
            FlaxBertLayer(config=self.config, dtype=self.dtype)
            for _ in range(self.config.num_hidden_layers)
        ]

    def __call__(self, x, attention_mask):
        for i, layer in enumerate(self.layers):
            # Add the pipeline start marker here
            mark_pipeline(name=str(i), mark_type='start')
            layer_outputs = layer(x, attention_mask)
            x = layer_outputs[0]
            # Add the pipeline end marker here
            mark_pipeline(name=str(i), mark_type='end')
        return x


model = EqualManualLayerBertLayerModel(config=bert_config)
state = create_train_state(rngkey, model, [x, attention_mask])

global_config_backup = global_config.backup()

# turn on manual stage plan
global_config.pipeline_stage_mode = "manual_gpipe"
# Layer-stage mapping
global_config.forward_stage_layer_ids = [[0], [1], [2, 3]]
# Physical mesh shape of each stage
global_config.sub_physical_mesh_shapes = [(1, 1), (1, 1), (1, 2)]
# Logical mesh shape of each stage
global_config.sub_logical_mesh_shapes = [(1, 1), (1, 1), (2, 1)]
# auto sharding option of each stage
global_config.submesh_autosharding_option_dicts = [{}, {}, {}]
executable = parallelize(train_step).get_executable(state, batch)
profile_and_pp_pipeshard_stats(executable)

executable.shutdown()
global_config.restore(global_config_backup)

###########################################
# Rematerialization with layer construction
# -----------------------------------------
#
# We provide a layer-based rematerialization.

model = EqualManualLayerBertLayerModel(config=bert_config)
state = create_train_state(rngkey, model, [x, attention_mask])


def get_train_step(remat_layer):

    def train_step(state, batch):

        # Set remat_layer in manual layer construction decorator here.
        # The same is true for automatic layer construction decorator.
        @manual_layer_construction(lift_markers=True, remat_layer=remat_layer)
        def loss_func(params):
            out = state.apply_fn(params, batch["x"], batch["attention_mask"])
            loss = jnp.mean((out - batch["y"])**2)
            return loss

        grads = alpa.grad(loss_func)(state.params)
        new_state = state.apply_gradients(grads=grads)
        return new_state

    return train_step


print(">>>>> With remat")
executable = parallelize(get_train_step(True)).get_executable(state, batch)
profile_and_pp_pipeshard_stats(executable)
executable.shutdown()
reset_state()
print(">>>>> Without remat")
executable = parallelize(get_train_step(False)).get_executable(state, batch)
profile_and_pp_pipeshard_stats(executable)
executable.shutdown()

###########################################
# The peak memory is significantly smaller when remat_layer is turned on.
#
# Moreover, we can remat at a fine-grained level, then do parallel at a relatively
# coarse-grained level. The example below remat at each Bert Layer, but do
# inter-operator parallelization for each two Bert Layers

from alpa import automatic_remat, automatic_layer_construction

model = BertLayerModel(config=bert_config)


def get_train_step(remat_layer):

    def train_step(state, batch):

        def loss_func(params):
            out = state.apply_fn(params, batch["x"], batch["attention_mask"])
            loss = jnp.mean((out - batch["y"])**2)
            return loss

        # Split the forward into 4 parts for remat
        if remat_layer:
            loss_func = automatic_remat(loss_func, layer_num=4)
        # Split the forward(remat-marked) into 2 parts for inter-operator parallel
        loss_func = automatic_layer_construction(loss_func, layer_num=2)
        grads = alpa.grad(loss_func)(state.params)
        new_state = state.apply_gradients(grads=grads)
        return new_state

    return train_step


print(">>>>> With remat")
state = create_train_state(rngkey, model, [x, attention_mask])
executable = parallelize(get_train_step(True)).get_executable(state, batch)
profile_and_pp_pipeshard_stats(executable)
executable.shutdown()
reset_state()
print(">>>>> Without remat")
executable = parallelize(get_train_step(False)).get_executable(state, batch)
profile_and_pp_pipeshard_stats(executable)
executable.shutdown()
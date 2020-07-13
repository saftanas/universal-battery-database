import numpy as np
import tensorflow as tf

from tensorflow.keras import Model

from Key import Key

from machine_learning.PrimitiveDictionaryLayer import PrimitiveDictionaryLayer
from machine_learning.loss_calculator_blackbox import calculate_q_loss
from machine_learning.incentives import (
    Level, Target, incentive_magnitude, incentive_combine,
)
from machine_learning.fnn_functions import (
    feedforward_nn_parameters, nn_call, create_derivatives,
    add_v_dep, add_current_dep,
)


class DegradationModel(Model):
    """
    The machine learning model of the long-term cycling data in this project.

    Notes:
        This version of Degradation Model has almost no internal structure.

    See Also:
        See the `call` method in this class for further information on using
            this class.
    """

    def __init__(
        self, depth, width, n_sample, options, cell_dict, cell_latent_flags,
        n_channels = 16, min_latent = 0.1,
    ):
        """
        Args:
            options: Used to access the incentive coefficients.
        """
        super(DegradationModel, self).__init__()

        # minimum latent
        self.min_latent = min_latent

        # TODO(harvey): decide the style of "number of x" for the whole project
        # number of samples
        self.n_sample = n_sample
        # incentive coefficients
        self.options = options

        # TODO(harvey): decide the style of "number of x" for the whole project
        # number of features
        self.num_feats = width

        # feedforward neural network for capacity
        self.nn_q = feedforward_nn_parameters(depth, width, finalize = True)

        self.cell_direct = PrimitiveDictionaryLayer(
            num_feats = self.num_feats, id_dict = cell_dict,
        )

        self.num_keys = self.cell_direct.num_keys

        # cell_latent_flags is a dict with cell_ids as keys.
        # latent_flags is a numpy array such that the indecies match cell_dict
        latent_flags = np.ones(
            (self.cell_direct.num_keys, 1), dtype = np.float32,
        )

        for cell_id in self.cell_direct.id_dict.keys():
            if cell_id in cell_latent_flags.keys():
                latent_flags[
                    self.cell_direct.id_dict[cell_id], 0,
                ] = cell_latent_flags[cell_id]

        self.cell_latent_flags = tf.constant(latent_flags)

        self.width = width
        self.n_channels = n_channels

    def call(self, params: dict, training = False) -> dict:
        """
        Call function for the Model during training or evaluation.

        Examples: TODO(harvey)

        Args:
            params: Contains -
                Cycle,
                Constant current,
                The end current of the previous step,
                The end voltage of the previous step,
                The end voltage of the current step,
                Indices,
                Voltage,
                Current
                S.V.I.T. grid,
                Count
            training: Flag for training or evaluation.
                True for training; False for evaluation.

        Returns:
            `{ Key.Pred.I_CC, Key.Pred.I_CV }`. During training, the
                dictionary also includes `{ Key.Loss.Q, Key.Loss.CELL }`.
        """

        cycle = params[Key.CYC]  # matrix; dim: [batch, 1]
        constant_current = params[Key.I_CC]  # matrix; dim: [batch, 1]
        end_current_prev = params[Key.I_PREV_END]  # matrix; dim: [batch, 1]
        end_voltage_prev = params[Key.V_PREV_END]  # matrix; dim: [batch, 1]
        end_voltage = params[Key.V_END]  # matrix; dim: [batch, 1]
        indices = params[Key.INDICES]  # batch of index; dim: [batch]
        voltage_tensor = params[Key.V_TENSOR]  # dim: [batch, voltages]
        current_tensor = params[Key.I_TENSOR]  # dim: [batch, voltages]
        svit_grid = params[Key.SVIT_GRID]
        count_matrix = params[Key.COUNT_MATRIX]

        feats_cell, _, _ = self.cell_from_indices(
            indices = indices, training = training, sample = False,
        )

        # duplicate cycles and others for all the voltages
        # dimensions are now [batch, voltages, features_cell]
        batch_count = cycle.shape[0]
        voltage_count = voltage_tensor.shape[1]
        current_count = current_tensor.shape[1]

        params = {
            Key.COUNT_BATCH: batch_count,
            Key.COUNT_V: voltage_count,
            Key.COUNT_I: current_count,

            Key.V: tf.reshape(voltage_tensor, [-1, 1]),
            Key.I_CV: tf.reshape(current_tensor, [-1, 1]),

            Key.CYC: cycle,
            Key.I_CC: constant_current,
            Key.I_PREV_END: end_current_prev,
            Key.V_PREV_END: end_voltage_prev,
            Key.CELL_FEAT: feats_cell,
            Key.V_END: end_voltage,

            Key.SVIT_GRID: svit_grid,
            Key.COUNT_MATRIX: count_matrix,
        }
        cc_capacity = self.cc_capacity(params, training = training)
        pred_cc_capacity = tf.reshape(cc_capacity, [-1, voltage_count])

        cv_capacity = self.cv_capacity(params, training = training)
        pred_cv_capacity = tf.reshape(cv_capacity, [-1, current_count])

        returns = {
            Key.Pred.I_CC: pred_cc_capacity,
            Key.Pred.I_CV: pred_cv_capacity,
        }

        if training:
            (
                sampled_vs, sampled_qs, sampled_cycles,
                sampled_constant_current, sampled_feats_cell, sampled_latent,
                sampled_svit_grid, sampled_count_matrix,
            ) = self.sample(
                svit_grid, batch_count, count_matrix, n_sample = self.n_sample,
            )

            q, q_der = create_derivatives(
                self.q_for_derivative,
                params = {
                    Key.CYC: sampled_cycles,
                    Key.V: sampled_vs,
                    Key.CELL_FEAT: sampled_feats_cell,
                    Key.I: sampled_constant_current,
                },
                der_params = {Key.V: 3, Key.CELL_FEAT: 2, Key.I: 3, Key.CYC: 3}
            )

            q_loss = calculate_q_loss(q, q_der, options = self.options)

            _, cell_loss, _ = self.cell_from_indices(
                indices = tf.range(self.cell_direct.num_keys, dtype = tf.int32),
                training = True,
                sample = False,
            )

            returns[Key.Loss.Q] = q_loss
            returns[Key.Loss.CELL] = cell_loss

        return returns

    def cell_from_indices(self, indices, training = True, sample = False):
        """ Cell from indices
        TODO(harvey, confusion): Need detailed explanation for what this
            function does.
            What are `indices`? What do the flags do?
        """
        feats_cell_direct, loss_cell = self.cell_direct(
            indices, training = training, sample = False,
        )

        fetched_latent_cell = tf.gather(
            self.cell_latent_flags, indices, axis = 0,
        )

        fetched_latent_cell = (
            self.min_latent + (1 - self.min_latent) * fetched_latent_cell
        )

        feats_cell = fetched_latent_cell * feats_cell_direct

        if training:
            loss_output_cell = incentive_magnitude(
                feats_cell, Target.Small, Level.Proportional,
            )
            loss_output_cell = tf.reduce_mean(
                loss_output_cell, axis = 1, keepdims = True,
            )

        else:
            loss_output_cell = None

        if sample:
            eps = tf.random.normal(
                shape = [feats_cell.shape[0], self.num_feats]
            )
            feats_cell += self.cell_direct.sample_epsilon * eps

        if training:
            loss = incentive_combine([
                (self.options["coeff_cell_output"], loss_output_cell),
            ])
        else:
            loss = 0.

        return feats_cell, loss, fetched_latent_cell

    def sample(self, svit_grid, batch_count, count_matrix, n_sample = 4 * 32):
        """
        Sample from all possible values of different variables.

        Args: TODO(harvey)
            svit_grid: multi-grid of (S, V, I, T).
            batch_count:
            count_matrix:
            n_sample:

        Returns:
            Sample values - voltages, capacities, cycles,
                constant current, cell features, latent, svit_grid,
                count_matrix
        """

        # NOTE(sam): this is an example of a forall.
        # (for all voltages, and all cell features)
        sampled_vs = tf.random.uniform(
            minval = 2.5, maxval = 5., shape = [n_sample, 1],
        )
        sampled_qs = tf.random.uniform(
            minval = -.25, maxval = 1.25, shape = [n_sample, 1],
        )
        sampled_cycles = tf.random.uniform(
            minval = -.1, maxval = 5., shape = [n_sample, 1],
        )
        sampled_constant_current = tf.random.uniform(
            minval = tf.math.log(0.001), maxval = tf.math.log(5.),
            shape = [n_sample, 1],
        )
        sampled_constant_current = tf.exp(sampled_constant_current)
        sampled_constant_current_sign = tf.random.uniform(
            minval = 0, maxval = 1, shape = [n_sample, 1], dtype = tf.int32,
        )
        sampled_constant_current_sign = tf.cast(
            sampled_constant_current_sign, dtype = tf.float32,
        )
        sampled_constant_current_sign = (
            1. * sampled_constant_current_sign
            - (1. - sampled_constant_current_sign)
        )

        sampled_constant_current = (
            sampled_constant_current_sign * sampled_constant_current
        )

        sampled_feats_cell, _, sampled_latent = self.cell_from_indices(
            indices = tf.random.uniform(
                maxval = self.cell_direct.num_keys,
                shape = [n_sample], dtype = tf.int32,
            ),
            training = False,
            sample = True,
        )
        sampled_feats_cell = tf.stop_gradient(sampled_feats_cell)

        sampled_svit_grid = tf.gather(
            svit_grid,
            indices = tf.random.uniform(
                minval = 0, maxval = batch_count,
                shape = [n_sample], dtype = tf.int32,
            ),
            axis = 0,
        )
        sampled_count_matrix = tf.gather(
            count_matrix,
            indices = tf.random.uniform(
                minval = 0, maxval = batch_count,
                shape = [n_sample], dtype = tf.int32,
            ),
            axis = 0,
        )

        return (
            sampled_vs, sampled_qs, sampled_cycles, sampled_constant_current,
            sampled_feats_cell, sampled_latent, sampled_svit_grid,
            sampled_count_matrix,
        )

    def cc_capacity(self, params: dict, training = True):
        """
        Compute constant-current capacity during training or evaluation.

        Args:
            params: Contains the parameters of constant-current capacity.
            training: Flag for training or evaluation.
                True for training; False for evaluation.

        Returns:
            Computed constant-current capacity.
        """

        q_0 = self.q_direct(
            cycle = params[Key.CYC],
            v = params[Key.V_PREV_END],
            feats_cell = params[Key.CELL_FEAT],
            current = params[Key.I_PREV_END],
            training = training,
        )

        q_1 = self.q_direct(
            cycle = add_v_dep(params[Key.CYC], params),
            v = params[Key.V],
            feats_cell = add_v_dep(
                params[Key.CELL_FEAT], params, params[Key.CELL_FEAT].shape[1],
            ),
            current = add_v_dep(params[Key.I_CC], params),
            training = training,
        )

        return q_1 - add_v_dep(q_0, params)

    def cv_capacity(self, params: dict, training = True):
        """
        Compute constant-voltage capacity during training or evaluation.

        Args:
            params: Parameters for computing constant-voltage (cv) capacity.
            training: Flag for training or evaluation.
                True for training; False for evaluation.

        Returns:
            Computed constant-voltage capacity.
        """

        q_0 = self.q_direct(
            cycle = params[Key.CYC],
            v = params[Key.V_PREV_END],
            feats_cell = params[Key.CELL_FEAT],
            current = params[Key.I_PREV_END],
            training = training,
        )

        # NOTE (sam): if there truly is no dependency on current for scale,
        # then we can restructure the code below.

        q_1 = self.q_direct(
            cycle = add_current_dep(params[Key.CYC], params),
            v = add_current_dep(params[Key.V_END], params),
            feats_cell = add_current_dep(
                params[Key.CELL_FEAT], params, params[Key.CELL_FEAT].shape[1],
            ),
            current = params[Key.I_CV],
            training = training,
        )

        return q_1 - add_current_dep(q_0, params)

    def q_direct(
        self, cycle, v, feats_cell, current, training = True,
    ):
        """
        Compute state of charge directly
            (receiving arguments directly without using `params`)

        Args: TODO(harvey)
            cycle: Cycle, often Key.CYC.
            v: Voltage
            feats_cell: Cell features.
            current: Current.
            training: Flag for training or evaluation.
                True for training; False for evaluation.

        Returns:
            Computed state of charge.
        """

        sigma = 0.1
        b, d, f = len(cycle), 3, 32

        input_vector = tf.concat(
            [cycle, v, current],
            axis = 1,
        )
        random_gaussian_matrix = tf.constant(
            np.random.normal(0, sigma, (d, f)),
            dtype = tf.float32,
        )
        dot_product = tf.einsum(
            'bd,df->bf',
            input_vector,
            random_gaussian_matrix,
        )

        dependencies = (
            tf.math.sin(2 * np.pi * dot_product),
            tf.math.cos(2 * np.pi * dot_product),
            feats_cell,
        )
        return tf.nn.elu(nn_call(self.nn_q, dependencies, training = training))

    def q_for_derivative(self, params: dict, training = True):
        """
        Wrapper function calling `q_direct`, to be passed in to
            `create_derivatives` to compute state of charge
            and its first derivative.

        Examples:
            ```python
            # state of charge and its first derivative
            q, q_der = create_derivatives(
                self.q_for_derivative,
                params = {
                    Key.CYC: sampled_cycles,
                    Key.V: sampled_vs,
                    Key.CELL_FEAT: sampled_features_cell,
                    Key.I: sampled_constant_current
                },
                der_params = {
                    Key.V: 3, Key.CELL_FEAT: 2, Key.I: 3, Key.CYC: 3,
                }
            )
            ```

        Args:
            params: Contains input parameters for computing state of charge (q).
            training: Flag for training or evaluation.
                True for training; False for evaluation.

        Returns:
            Computed state of charge; same as that for `q_direct`.
        """

        return self.q_direct(
            cycle = params[Key.CYC],
            feats_cell = params[Key.CELL_FEAT],
            v = params[Key.V],
            current = params[Key.I],
            training = training,
        )

    @tf.function
    def test_all_voltages(
        self, cycle, constant_current, end_current_prev, end_voltage_prev,
        end_voltage, cell_id_index, voltages, currents, svit_grid, count_matrix,
    ):

        expanded_cycle = tf.expand_dims(cycle, axis = 1)
        expanded_constant_current = tf.tile(
            tf.reshape(constant_current, [1, 1]),
            [cycle.shape[0], 1],
        )
        expanded_end_current_prev = tf.tile(
            tf.reshape(end_current_prev, [1, 1]),
            [cycle.shape[0], 1],
        )
        expanded_end_voltage_prev = tf.tile(
            tf.reshape(end_voltage_prev, [1, 1]),
            [cycle.shape[0], 1],
        )
        expanded_end_voltage = tf.tile(
            tf.reshape(end_voltage, [1, 1]),
            [cycle.shape[0], 1],
        )

        indices = tf.tile(
            tf.expand_dims(cell_id_index, axis = 0),
            [cycle.shape[0]],
        )

        expanded_svit_grid = tf.tile(
            tf.expand_dims(svit_grid, axis = 0),
            [cycle.shape[0], 1, 1, 1, 1, 1],
        )
        expanded_count_matrix = tf.tile(
            tf.expand_dims(count_matrix, axis = 0),
            [cycle.shape[0], 1, 1, 1, 1, 1],
        )

        return self.call(
            {
                Key.CYC: expanded_cycle,
                Key.I_CC: expanded_constant_current,
                Key.I_PREV_END: expanded_end_current_prev,
                Key.V_PREV_END: expanded_end_voltage_prev,
                Key.V_END: expanded_end_voltage,
                Key.INDICES: indices,
                Key.V_TENSOR: tf.tile(
                    tf.reshape(voltages, [1, -1]), [cycle.shape[0], 1],
                ),
                Key.I_TENSOR: tf.tile(
                    tf.reshape(currents, shape = [1, -1]), [cycle.shape[0], 1],
                ),
                Key.SVIT_GRID: expanded_svit_grid,
                Key.COUNT_MATRIX: expanded_count_matrix,
            },
            training = False,
        )

    @tf.function
    def test_single_voltage(
        self, cycle, v, constant_current, end_current_prev, end_voltage_prev,
        end_voltage, currents, cell_id_index, svit_grid, count_matrix
    ):

        expanded_cycle = tf.expand_dims(cycle, axis = 1)
        expanded_constant_current = tf.tile(
            tf.reshape(constant_current, [1, 1]),
            [cycle.shape[0], 1],
        )
        expanded_end_current_prev = tf.tile(
            tf.reshape(end_current_prev, [1, 1]),
            [cycle.shape[0], 1],
        )
        expanded_end_voltage_prev = tf.tile(
            tf.reshape(end_voltage_prev, [1, 1]),
            [cycle.shape[0], 1],
        )
        expanded_end_voltage = tf.tile(
            tf.reshape(end_voltage, [1, 1]),
            [cycle.shape[0], 1],
        )

        indices = tf.tile(
            tf.expand_dims(cell_id_index, axis = 0),
            [cycle.shape[0]],
        )

        expanded_svit_grid = tf.tile(
            tf.expand_dims(svit_grid, axis = 0),
            [cycle.shape[0], 1, 1, 1, 1, 1],
        )
        expanded_count_matrix = tf.tile(
            tf.expand_dims(count_matrix, axis = 0),
            [cycle.shape[0], 1, 1, 1, 1, 1],
        )

        return self.call(
            {
                Key.CYC: expanded_cycle,
                Key.I_CC: expanded_constant_current,
                Key.I_PREV_END: expanded_end_current_prev,
                Key.V_PREV_END: expanded_end_voltage_prev,
                Key.V_END: expanded_end_voltage,
                Key.INDICES: indices,
                Key.V_TENSOR: tf.tile(
                    tf.reshape(v, [1, 1]), [cycle.shape[0], 1]
                ),
                Key.I_TENSOR: tf.tile(
                    tf.reshape(currents, shape = [1, -1]), [cycle.shape[0], 1],
                ),
                Key.SVIT_GRID: expanded_svit_grid,
                Key.COUNT_MATRIX: expanded_count_matrix,
            },
            training = False
        )

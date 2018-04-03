from copy import copy
from functools import reduce

import numpy as np
import tensorflow as tf
import tensorflow.contrib as tc

from baselines import logger
from baselines.common.mpi_adam import MpiAdam
import baselines.common.tf_util as U
from baselines.common.mpi_running_mean_std import RunningMeanStd
from mpi4py import MPI
import cv2

from pathlib import Path
home = str(Path.home())

def normalize(x, stats):
    if stats is None:
        return x
    return (x - stats.mean) / stats.std


def denormalize(x, stats):
    if stats is None:
        return x
    return x * stats.std + stats.mean

def reduce_std(x, axis=None, keepdims=False):
    return tf.sqrt(reduce_var(x, axis=axis, keepdims=keepdims))

def reduce_var(x, axis=None, keepdims=False):
    m = tf.reduce_mean(x, axis=axis, keep_dims=True)
    devs_squared = tf.square(x - m)
    return tf.reduce_mean(devs_squared, axis=axis, keep_dims=keepdims)

def get_target_updates(vars, target_vars, tau):
    logger.info('setting up target updates ...')
    soft_updates = []
    init_updates = []
    assert len(vars) == len(target_vars)
    for var, target_var in zip(vars, target_vars):
        logger.info('  {} <- {}'.format(target_var.name, var.name))
        init_updates.append(tf.assign(target_var, var))
        soft_updates.append(tf.assign(target_var, (1. - tau) * target_var + tau * var))
    assert len(init_updates) == len(vars)
    assert len(soft_updates) == len(vars)
    return tf.group(*init_updates), tf.group(*soft_updates)


def get_perturbed_actor_updates(actor, perturbed_actor, param_noise_stddev):
    assert len(actor.vars) == len(perturbed_actor.vars)
    assert len(actor.perturbable_vars) == len(perturbed_actor.perturbable_vars)

    updates = []
    for var, perturbed_var in zip(actor.vars, perturbed_actor.vars):
        if var in actor.perturbable_vars:
            logger.info('  {} <- {} + noise'.format(perturbed_var.name, var.name))
            updates.append(tf.assign(perturbed_var, var + tf.random_normal(tf.shape(var), mean=0., stddev=param_noise_stddev)))
        else:
            logger.info('  {} <- {}'.format(perturbed_var.name, var.name))
            updates.append(tf.assign(perturbed_var, var))
    assert len(updates) == len(actor.vars)
    return tf.group(*updates)


class DDPG(object):
    def __init__(self, actor, critic, memory, observation_shape, action_shape, state_shape, aux_shape, param_noise=None, action_noise=None,
        gamma=0.99, tau=0.001, normalize_returns=False, enable_popart=False, normalize_observations=True, normalize_state=True, normalize_aux=True,
        batch_size=128, observation_range=(0., 1.), action_range=(-1., 1.), state_range=(-4, 4), return_range=(-np.inf, np.inf), aux_range=(-10, 10),
        adaptive_param_noise=True, adaptive_param_noise_policy_threshold=.1,
        critic_l2_reg=0.001, actor_lr=1e-4, critic_lr=1e-3, clip_norm=None, reward_scale=1., replay_beta=0.4,lambda_1step=1.0, lambda_nstep=1.0, nsteps=10, run_name="unnamed_run", lambda_pretrain=0.0):

        # Inputs.
        self.obs0 = tf.placeholder(tf.float32, shape=(None,) + observation_shape, name='obs0')
        self.obs1 = tf.placeholder(tf.float32, shape=(None,) + observation_shape, name='obs1')

        self.goal = tf.placeholder(tf.float32, shape=(None,) + state_shape, name='goal')
        self.state0 = tf.placeholder(tf.float32, shape=(None,) + state_shape, name='state0')
        self.state1 = tf.placeholder(tf.float32, shape=(None,) + state_shape, name='state1')

        self.terminals1 = tf.placeholder(tf.float32, shape=(None, 1), name='terminals1')
        self.rewards = tf.placeholder(tf.float32, shape=(None, 1), name='rewards')
        self.actions = tf.placeholder(tf.float32, shape=(None,) + action_shape, name='actions')
        self.critic_target = tf.placeholder(tf.float32, shape=(None, 1), name='critic_target')

        self.nstep_steps = tf.placeholder(tf.float32, shape=(None, 1), name='nstep_reached')
        self.nstep_critic_target = tf.placeholder(tf.float32, shape=(None, 1), name='nstep_critic_target')


        self.param_noise_stddev = tf.placeholder(tf.float32, shape=(), name='param_noise_stddev')

        self.aux0 = tf.placeholder(tf.float32, shape=(None,) + aux_shape, name='aux0')
        self.aux1 = tf.placeholder(tf.float32, shape=(None,) + aux_shape, name='aux1')

        self.pretraining_tf = tf.placeholder(tf.float32, shape=(None, 1),
                                             name='pretraining_tf')  # whether we use pre training or not
        self.lambda_pretrain_in = tf.placeholder(tf.float32, shape=None, name='lamdba_pretrain_in')
        # Parameters.

        self.aux_shape = aux_shape
        self.gamma = gamma
        self.tau = tau
        self.memory = memory
        self.normalize_observations = normalize_observations
        self.normalize_returns = normalize_returns
        self.normalize_state = normalize_state
        self.normalize_aux = normalize_aux
        self.action_noise = action_noise
        self.param_noise = param_noise
        self.action_range = action_range
        self.return_range = return_range
        self.observation_range = observation_range
        self.critic = critic
        self.actor = actor
        self.actor_lr = actor_lr
        self.state_range = state_range
        self.aux_range = aux_range
        self.critic_lr = critic_lr
        self.clip_norm = clip_norm
        self.enable_popart = enable_popart
        self.reward_scale = reward_scale
        self.batch_size = batch_size
        self.stats_sample = None
        self.critic_l2_reg = critic_l2_reg
        self.lambda_nstep = lambda_nstep
        self.lambda_1step = lambda_1step
        self.nsteps = nsteps
        self.beta = replay_beta
        self.run_name = run_name
        self.lambda_pretrain = lambda_pretrain

        # Observation normalization.
        if self.normalize_observations:
            with tf.variable_scope('obs_rms'):
                self.obs_rms = RunningMeanStd(shape=observation_shape)
        else:
            self.obs_rms = None

        if self.normalize_state:
            with tf.variable_scope('state_rms'):
                self.state_rms = RunningMeanStd(shape=state_shape)
        else:
            self.state_rms

        if self.normalize_aux:
            with tf.variable_scope('normalize_aux'):
                self.aux_rms = RunningMeanStd(shape=aux_shape)
        else:
            self.aux_rms

        normalized_obs0 = tf.clip_by_value(normalize(self.obs0, self.obs_rms),
            self.observation_range[0], self.observation_range[1])
        normalized_obs1 = tf.clip_by_value(normalize(self.obs1, self.obs_rms),
            self.observation_range[0], self.observation_range[1])

        normalized_state0 = tf.clip_by_value(normalize(self.state0, self.state_rms),
            self.state_range[0], self.state_range[1])
        normalized_state1 = tf.clip_by_value(normalize(self.state1, self.state_rms),
            self.state_range[0], self.state_range[1])

        normalized_goal = tf.clip_by_value(normalize(self.goal, self.state_rms),
            self.state_range[0], self.state_range[1])

        normalized_aux0 = tf.clip_by_value(normalize(self.aux0, self.aux_rms),
                self.aux_range[0], self.aux_range[1])
        normalized_aux1 = tf.clip_by_value(normalize(self.aux1, self.aux_rms),
                self.aux_range[0], self.aux_range[1])
        normalized_goal = self.goal

        # Return normalization.
        if self.normalize_returns:
            with tf.variable_scope('ret_rms'):
                self.ret_rms = RunningMeanStd()
        else:
            self.ret_rms = None

        # Create target networks.
        target_actor = copy(actor)
        target_actor.name = 'target_actor'
        self.target_actor = target_actor
        target_critic = copy(critic)
        target_critic.name = 'target_critic'
        self.target_critic = target_critic

        # Create networks and core TF parts that are shared across setup parts.
        self.actor_tf = actor(normalized_obs0, normalized_aux0)
        self.normalized_critic_tf = critic(normalized_state0, normalized_goal, self.actions, normalized_aux0)
        self.critic_tf = denormalize(tf.clip_by_value(self.normalized_critic_tf, self.return_range[0], self.return_range[1]), self.ret_rms)
        self.normalized_critic_with_actor_tf = critic(normalized_state0, normalized_goal, self.actor_tf, normalized_aux0, reuse=True)
        self.critic_with_actor_tf = denormalize(tf.clip_by_value(self.normalized_critic_with_actor_tf, self.return_range[0], self.return_range[1]), self.ret_rms)
        Q_obs1 = denormalize(target_critic(normalized_state1, normalized_goal, target_actor(normalized_obs1, normalized_aux1), normalized_aux1), self.ret_rms)
        self.target_Q = self.rewards + (1. - self.terminals1) * tf.pow(gamma, self.nstep_steps) * Q_obs1

        self.importance_weights = tf.placeholder(tf.float32, shape=(None, 1), name='importance_weights')



        # pretrain stuff
        action_diffs = self.action_diffs = tf.reduce_mean(tf.square(self.actions - self.actor_tf), 1)  # reduce mean of the actions so that we get shape (None, 1)

        margin_limit = 0.01 # original = 0.1
        tolerance = 0.01  # original = 0.001

        self.margin_func = self.pretraining_tf * (margin_limit * tf.square(action_diffs)) / (tf.square(action_diffs) + tolerance)

        self.max_margin_func = self.pretraining_tf * tf.maximum(self.normalized_critic_with_actor_tf + self.margin_func - self.critic_tf, 0)

        # This scales the loss relative to the number of demonstrations
        self.pretrain_loss = self.lambda_pretrain * (tf.reduce_sum(self.max_margin_func) / (tf.reduce_sum(self.pretraining_tf) + 1e-6))
        # end pretrain stuff


        # Set up parts.
        if self.param_noise is not None:
            self.setup_param_noise(normalized_obs0, normalized_aux0)
        self.setup_actor_optimizer()
        self.setup_critic_optimizer()
        if self.normalize_returns and self.enable_popart:
            self.setup_popart()
        self.setup_stats()
        self.setup_target_network_updates()
        self.setup_summaries()

    def setup_target_network_updates(self):
        actor_init_updates, actor_soft_updates = get_target_updates(self.actor.vars, self.target_actor.vars, self.tau)
        critic_init_updates, critic_soft_updates = get_target_updates(self.critic.vars, self.target_critic.vars, self.tau)
        self.target_init_updates = [actor_init_updates, critic_init_updates]
        self.target_soft_updates = [actor_soft_updates, critic_soft_updates]

    def setup_param_noise(self, normalized_obs0, normalized_aux0):
        assert self.param_noise is not None

        # Configure perturbed actor.
        param_noise_actor = copy(self.actor)
        param_noise_actor.name = 'param_noise_actor'
        self.perturbed_actor_tf = param_noise_actor(normalized_obs0, normalized_aux0)
        logger.info('setting up param noise')
        self.perturb_policy_ops = get_perturbed_actor_updates(self.actor, param_noise_actor, self.param_noise_stddev)

        # Configure separate copy for stddev adoption.
        adaptive_param_noise_actor = copy(self.actor)
        adaptive_param_noise_actor.name = 'adaptive_param_noise_actor'
        adaptive_actor_tf = adaptive_param_noise_actor(normalized_obs0, normalized_aux0)
        self.perturb_adaptive_policy_ops = get_perturbed_actor_updates(self.actor, adaptive_param_noise_actor, self.param_noise_stddev)
        self.adaptive_policy_distance = tf.sqrt(tf.reduce_mean(tf.square(self.actor_tf - adaptive_actor_tf)))

    def setup_actor_optimizer(self):
        logger.info('setting up actor optimizer')

        demo_better_than_critic = self.critic_tf < self.critic_with_actor_tf
        demo_better_than_critic = self.pretraining_tf * tf.cast(demo_better_than_critic, tf.float32)

        # self.actor_loss = -tf.reduce_mean(self.critic_with_actor_tf) + (tf.reduce_sum(demo_better_than_critic * self.action_diffs) / (tf.reduce_sum(self.pretraining_tf) + 1e-6))
        self.actor_loss = -tf.reduce_mean(self.critic_with_actor_tf)

        self.number_of_demos_better = tf.reduce_sum(demo_better_than_critic)


        actor_shapes = [var.get_shape().as_list() for var in self.actor.trainable_vars]
        actor_nb_params = sum([reduce(lambda x, y: x * y, shape) for shape in actor_shapes])
        logger.info('  actor shapes: {}'.format(actor_shapes))
        logger.info('  actor params: {}'.format(actor_nb_params))
        self.actor_grads = U.flatgrad(self.actor_loss, self.actor.trainable_vars, clip_norm=self.clip_norm)
        self.actor_optimizer = MpiAdam(var_list=self.actor.trainable_vars,
            beta1=0.9, beta2=0.999, epsilon=1e-08)

    def setup_critic_optimizer(self):
        logger.info('setting up critic optimizer')

        normalized_critic_target_tf = tf.clip_by_value(normalize(self.critic_target, self.ret_rms), self.return_range[0], self.return_range[1])

        normalized_nstep_critic_target_tf = tf.clip_by_value(normalize(self.nstep_critic_target, self.ret_rms), self.return_range[0], self.return_range[1])

        td_error = tf.square(self.normalized_critic_tf - normalized_critic_target_tf)
        self.step_1_td_loss = tf.reduce_mean(self.importance_weights * td_error) * self.lambda_1step

        nstep_td_error = tf.square(self.normalized_critic_tf - normalized_nstep_critic_target_tf)
        self.n_step_td_loss = tf.reduce_mean(self.importance_weights * nstep_td_error) * self.lambda_nstep

        self.td_error = td_error + nstep_td_error
        #self.td_error = td_error
        self.critic_loss = self.step_1_td_loss + self.n_step_td_loss + self.pretrain_loss

        if self.critic_l2_reg > 0.:
            critic_reg_vars = [var for var in self.critic.trainable_vars if 'kernel' in var.name and 'output' not in var.name]
            for var in critic_reg_vars:
                logger.info('  regularizing: {}'.format(var.name))
            logger.info('  applying l2 regularization with {}'.format(self.critic_l2_reg))
            critic_reg = tc.layers.apply_regularization(
                tc.layers.l2_regularizer(self.critic_l2_reg),
                weights_list=critic_reg_vars
            )
            self.critic_loss += critic_reg
        critic_shapes = [var.get_shape().as_list() for var in self.critic.trainable_vars]
        critic_nb_params = sum([reduce(lambda x, y: x * y, shape) for shape in critic_shapes])
        logger.info('  critic shapes: {}'.format(critic_shapes))
        logger.info('  critic params: {}'.format(critic_nb_params))
        self.critic_grads = U.flatgrad(self.critic_loss, self.critic.trainable_vars, clip_norm=self.clip_norm)
        self.critic_optimizer = MpiAdam(var_list=self.critic.trainable_vars,
            beta1=0.9, beta2=0.999, epsilon=1e-08)

    def setup_popart(self):
        # See https://arxiv.org/pdf/1602.07714.pdf for details.
        self.old_std = tf.placeholder(tf.float32, shape=[1], name='old_std')
        new_std = self.ret_rms.std
        self.old_mean = tf.placeholder(tf.float32, shape=[1], name='old_mean')
        new_mean = self.ret_rms.mean

        self.renormalize_Q_outputs_op = []
        for vs in [self.critic.output_vars, self.target_critic.output_vars]:
            assert len(vs) == 2
            M, b = vs
            assert 'kernel' in M.name
            assert 'bias' in b.name
            assert M.get_shape()[-1] == 1
            assert b.get_shape()[-1] == 1
            self.renormalize_Q_outputs_op += [M.assign(M * self.old_std / new_std)]
            self.renormalize_Q_outputs_op += [b.assign((b * self.old_std + self.old_mean - new_mean) / new_std)]



    def setup_summaries(self):
        tf.summary.scalar("actor_loss", self.actor_loss)
        tf.summary.scalar("critic_loss", self.critic_loss)
        tf.summary.scalar("1step_loss", self.step_1_td_loss)
        tf.summary.scalar("nstep_loss", self.n_step_td_loss)

        tf.summary.scalar("percentage_of_demonstrations", tf.reduce_sum(self.pretraining_tf) / self.batch_size)
        tf.summary.scalar("margin_func_mean", tf.reduce_mean(self.margin_func))
        tf.summary.scalar("number_of_demos_better_than_actor", self.number_of_demos_better)
        tf.summary.histogram("margin_func", self.margin_func)
        tf.summary.histogram("pretrain_samples", self.pretraining_tf)
        tf.summary.histogram("margin_func_max", self.max_margin_func)

        self.scalar_summaries = tf.summary.merge_all()
        # reward
        self.r_plot_in = tf.placeholder(tf.float32, name='r_plot_in')
        self.r_plot = tf.summary.scalar("returns", self.r_plot_in)
        self.r_plot_in_eval = tf.placeholder(tf.float32, name='r_plot_in_eval')
        self.r_plot_eval = tf.summary.scalar("returns_eval", self.r_plot_in_eval)
        self.writer = tf.summary.FileWriter(home + '/fyp_summaries/'+ self.run_name, graph=tf.get_default_graph())


    def save_reward(self, r, ep):
        summary = self.sess.run(self.r_plot, feed_dict={self.r_plot_in: r})
        self.writer.add_summary(summary, ep)

    def save_eval_reward(self, r, ep):
        summary = self.sess.run(self.r_plot_eval, feed_dict={self.r_plot_in_eval: r})
        self.writer.add_summary(summary, ep)

    def setup_stats(self):
        ops = []
        names = []

        if self.normalize_returns:
            ops += [self.ret_rms.mean, self.ret_rms.std]
            names += ['ret_rms_mean', 'ret_rms_std']

        if self.normalize_observations:
            ops += [tf.reduce_mean(self.obs_rms.mean), tf.reduce_mean(self.obs_rms.std)]
            names += ['obs_rms_mean', 'obs_rms_std']

        ops += [tf.reduce_mean(self.critic_tf)]
        names += ['reference_Q_mean']
        ops += [reduce_std(self.critic_tf)]
        names += ['reference_Q_std']

        ops += [tf.reduce_mean(self.critic_with_actor_tf)]
        names += ['reference_actor_Q_mean']
        ops += [reduce_std(self.critic_with_actor_tf)]
        names += ['reference_actor_Q_std']

        ops += [tf.reduce_mean(self.actor_tf)]
        names += ['reference_action_mean']
        ops += [reduce_std(self.actor_tf)]
        names += ['reference_action_std']

        if self.param_noise:
            ops += [tf.reduce_mean(self.perturbed_actor_tf)]
            names += ['reference_perturbed_action_mean']
            ops += [reduce_std(self.perturbed_actor_tf)]
            names += ['reference_perturbed_action_std']

        self.stats_ops = ops
        self.stats_names = names

    def pi(self, obs, aux, apply_noise=True, compute_Q=True):
        if self.param_noise is not None and apply_noise:
            actor_tf = self.perturbed_actor_tf
        else:
            actor_tf = self.actor_tf
        feed_dict = {self.obs0: [obs], self.aux0: [aux]}
        if compute_Q:

            # action, q = self.sess.run([actor_tf, self.critic_with_actor_tf], feed_dict=feed_dict)
            action, q = self.sess.run(actor_tf, feed_dict=feed_dict), 137.0

        else:
            action = self.sess.run(actor_tf, feed_dict=feed_dict)
            q = None
        action = action.flatten()
        if self.action_noise is not None and apply_noise:
            noise = self.action_noise()
            assert noise.shape == action.shape
            action += noise
        action = np.clip(action, self.action_range[0], self.action_range[1])
        return action, q

    def store_transition(self, state, obs0, action, reward, state1, obs1, terminal1, goal, goalobs, aux0, aux1, demo=False):
        reward *= self.reward_scale
        if demo:
            self.memory.append_demonstration(state, obs0, action, reward, state1, obs1, terminal1, goal, goalobs, aux0, aux1)
        else:
            self.memory.append(state, obs0, action, reward, state1, obs1, terminal1, goal, goalobs, aux0, aux1)
        if self.normalize_observations:
            self.obs_rms.update(np.array([obs0]))

        if self.normalize_state:
            self.state_rms.update(np.array([state]))

        if self.normalize_aux:
            self.aux_rms.update(np.array([aux0]))

    def train(self, iteration, pretrain=False):
        # Get a batch.
        batch, nstep_batch, percentage = self.memory.sample_rollout(batch_size=self.batch_size, nsteps=self.nsteps, beta=self.beta, gamma=self.gamma, pretrain=pretrain)
        if self.normalize_returns and self.enable_popart:
            raise Exception("Not implemented")
            old_mean, old_std, target_Q = self.sess.run([self.ret_rms.mean, self.ret_rms.std, self.target_Q], feed_dict={
                self.obs1: batch['obs1'],
                self.state1: batch['states1'],
                self.goal: batch['goals'],
                self.rewards: batch['rewards'],
                self.terminals1: batch['terminals1'].astype('float32'),
                self.aux1: batch['aux1']
            })
            self.ret_rms.update(target_Q.flatten())
            self.sess.run(self.renormalize_Q_outputs_op, feed_dict={
                self.old_std : np.array([old_std]),
                self.old_mean : np.array([old_mean]),
            })

            # Run sanity check. Disabled by default since it slows down things considerably.
            # print('running sanity check')
            # target_Q_new, new_mean, new_std = self.sess.run([self.target_Q, self.ret_rms.mean, self.ret_rms.std], feed_dict={
            #     self.obs1: batch['obs1'],
            #     self.rewards: batch['rewards'],
            #     self.terminals1: batch['terminals1'].astype('float32'),
            # })
            # print(target_Q_new, target_Q, new_mean, new_std)
            # assert (np.abs(target_Q - target_Q_new) < 1e-3).all()
        else:
            target_Q_1step = self.sess.run(self.target_Q, feed_dict={
                self.obs1: batch['obs1'],
                self.state1: batch['states1'],
                self.aux1: batch['aux1'],
                self.goal: batch['goals'],
                self.rewards: batch['rewards'],
                self.terminals1: batch['terminals1'].astype('float32'),
                self.nstep_steps: np.ones((self.batch_size, 1)),
            })

            target_Q_nstep = self.sess.run(self.target_Q, feed_dict={
                self.obs1: batch['obs1'],
                self.state1: batch['states1'],
                self.aux1: batch['aux1'],
                self.goal: batch['goals'],
                self.rewards: batch['rewards'],
                self.nstep_steps: nstep_batch['step_reached'],
                self.terminals1: batch['terminals1'].astype('float32'),
            })

        # Get all gradients and perform a synced update.

        ops = [self.actor_grads, self.actor_loss, self.critic_grads, self.critic_loss, self.td_error, self.scalar_summaries, self.pretrain_loss]
        actor_grads, actor_loss, critic_grads, critic_loss, td_errors, scalar_summaries, pretrain_loss = self.sess.run(ops, feed_dict={
            self.obs0: batch['obs0'],
            self.importance_weights: batch['weights'],
            self.state0: batch['states0'],
            self.aux0: batch['aux0'],
            self.goal: batch['goals'],
            self.actions: batch['actions'],
            self.critic_target: target_Q_1step,
            self.nstep_critic_target: target_Q_nstep,
            self.pretraining_tf: batch['demos'].astype('float32'),
            self.importance_weights: batch['weights'],
        })
        self.memory.update_priorities(batch['idxes'], td_errors)
        self.actor_optimizer.update(actor_grads, stepsize=self.actor_lr)
        self.critic_optimizer.update(critic_grads, stepsize=self.critic_lr)
        self.writer.add_summary(scalar_summaries, iteration)
        return critic_loss, actor_loss

    def set_sess(self, sess):
        self.sess = sess

    def sync_optimizers(self):
        self.actor_optimizer.sync()
        self.critic_optimizer.sync()
        self.sess.run(self.target_init_updates)

    def initialize(self):
        self.sess.run(tf.global_variables_initializer())

    def update_target_net(self):
        self.sess.run(self.target_soft_updates)

    def get_stats(self):
        if self.stats_sample is None:
            # Get a sample and keep that fixed for all further computations.
            # This allows us to estimate the change in value for the same set of inputs.
            # TODO
            self.stats_sample = self.memory.sample(batch_size=self.batch_size, beta=0.4)
        values = self.sess.run(self.stats_ops, feed_dict={
            self.obs0: self.stats_sample['obs0'],
            self.actions: self.stats_sample['actions'],
            self.goal: self.stats_sample['goals'],
            self.aux0: self.stats_sample['aux0'],
            self.state0: self.stats_sample['states0'],
        })

        names = self.stats_names[:]
        assert len(names) == len(values)
        stats = dict(zip(names, values))

        if self.param_noise is not None:
            stats = {**stats, **self.param_noise.get_stats()}

        return stats

    def adapt_param_noise(self):
        if self.param_noise is None:
            return 0.

        # Perturb a separate copy of the policy to adjust the scale for the next "real" perturbation.
        batch = self.memory.sample(batch_size=self.batch_size, beta=0.4)
        self.sess.run(self.perturb_adaptive_policy_ops, feed_dict={
            self.param_noise_stddev: self.param_noise.current_stddev,
        })
        distance = self.sess.run(self.adaptive_policy_distance, feed_dict={
            self.obs0: batch['obs0'],
            self.aux0: batch['aux0'],
            self.param_noise_stddev: self.param_noise.current_stddev,
        })

        mean_distance = MPI.COMM_WORLD.allreduce(distance, op=MPI.SUM) / MPI.COMM_WORLD.Get_size()
        self.param_noise.adapt(mean_distance)
        return mean_distance

    def reset(self):
        # Reset internal state after an episode is complete.
        if self.action_noise is not None:
            self.action_noise.reset()
        if self.param_noise is not None:
            self.sess.run(self.perturb_policy_ops, feed_dict={
                self.param_noise_stddev: self.param_noise.current_stddev,
            })




    def write_summary(self, summary):
        agent_summary = {
            "gamma" : self.gamma,
            "tau" : self.tau,
            "normalize_observations" : self.normalize_observations,
            "normalize_returns" : self.normalize_returns,
            "normalize_state" : self.normalize_state,
            "normalize_aux" : self.normalize_aux,
            "action_noise" : self.action_noise,
            "param_noise" : self.param_noise,
            "action_range" : self.action_range,
            "return_range" : self.return_range,
            "observation_range" : self.observation_range,
            "actor_lr" : self.actor_lr,
            "state_range" : self.state_range,
            "critic_lr" : self.critic_lr,
            "clip_norm" : self.clip_norm,
            "enable_popart" : self.enable_popart,
            "reward_scale" : self.reward_scale,
            "batch_size" : self.batch_size,
            "critic_l2_reg" : self.critic_l2_reg,
            "lambda_nstep" : self.lambda_nstep,
            "lambda_1step" : self.lambda_1step,
            "nsteps" : self.nsteps,
            "beta" : self.beta,
            "run_name" : self.run_name,
            "lambda_pretrain" : self.lambda_pretrain,
        }
        summary["agent_summary"] = agent_summary
        md_string = self._markdownize_summary(summary)
        summary_op = tf.summary.text("param_info", tf.convert_to_tensor(md_string))
        text = self.sess.run(summary_op)
        self.writer.add_summary(text)
        self.writer.flush()
        print(md_string)

    def _markdownize_summary(self, data):
        result = []
        for section, params in data.items():
            result.append("### " + section)
            for param, value in params.items():
                result.append("* {} : {}".format(str(param), str(value)))
        return "\n".join(result)

import os.path as osp
import time
import joblib
import numpy as np
import tensorflow as tf
from baselines import logger

from baselines.common import set_global_seeds, explained_variance
from baselines.common.runners import AbstractEnvRunner
from baselines.common import tf_util

from baselines.a2c.utils import discount_with_dones
from baselines.a2c.utils import Scheduler, make_path, find_trainable_variables
from baselines.a2c.utils import cat_entropy, mse

class Model(object):

    def __init__(self, policy, ob_space, ac_space, nenvs, nsteps,
            ent_coef=0.01, vf_coef=0.5, max_grad_norm=0.5, lr=7e-4,
            alpha=0.99, epsilon=1e-5, total_timesteps=int(80e6), lrschedule='linear'):

        sess = tf_util.make_session()
        nbatch = nenvs*nsteps

        A = tf.placeholder(tf.int32, [nbatch])
        ADV = tf.placeholder(tf.float32, [nbatch])
        R = tf.placeholder(tf.float32, [nbatch])
        LR = tf.placeholder(tf.float32, [])

        step_model = policy(sess, ob_space, ac_space, nenvs, 1, reuse=False)
        train_model = policy(sess, ob_space, ac_space, nenvs*nsteps, nsteps, reuse=True)

        neglogpac = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=train_model.pi, labels=A)
        pg_loss = tf.reduce_mean(ADV * neglogpac)
        vf_loss = tf.reduce_mean(mse(tf.squeeze(train_model.vf), R))
        entropy = tf.reduce_mean(cat_entropy(train_model.pi))
        loss = pg_loss - entropy*ent_coef + vf_loss * vf_coef

        params = find_trainable_variables("model")
        grads = tf.gradients(loss, params)
        if max_grad_norm is not None:
            grads, grad_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        grads = list(zip(grads, params))
        trainer = tf.train.RMSPropOptimizer(learning_rate=LR, decay=alpha, epsilon=epsilon)
        _train = trainer.apply_gradients(grads)

        lr = Scheduler(v=lr, nvalues=total_timesteps, schedule=lrschedule)

        def train(obs, states, rewards, masks, actions, values):
            advs = rewards - values
            for step in range(len(obs)):
                cur_lr = lr.value()
            td_map = {train_model.X:obs, A:actions, ADV:advs, R:rewards, LR:cur_lr}
            if states is not None:
                td_map[train_model.S] = states
                td_map[train_model.M] = masks
            policy_loss, value_loss, policy_entropy, _ = sess.run(
                [pg_loss, vf_loss, entropy, _train],
                td_map
            )
            return policy_loss, value_loss, policy_entropy

        def save(save_path):
            ps = sess.run(params)
            make_path(osp.dirname(save_path))
            joblib.dump(ps, save_path)

        def load(load_path):
            loaded_params = joblib.load(load_path)
            restores = []
            for p, loaded_p in zip(params, loaded_params):
                restores.append(p.assign(loaded_p))
            sess.run(restores)

        self.train = train
        self.train_model = train_model
        self.step_model = step_model
        self.step = step_model.step
        self.value = step_model.value
        self.initial_state = step_model.initial_state
        self.save = save
        self.load = load
        tf.global_variables_initializer().run(session=sess)

class Runner(AbstractEnvRunner):

    def __init__(self, env, model, nsteps=5, gamma=0.99):
        super().__init__(env=env, model=model, nsteps=nsteps)
        self.gamma = gamma

    def run(self):
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones = [],[],[],[],[]
        mb_states = self.states
        for n in range(self.nsteps):
            actions, values, states, _ = self.model.step(self.obs, self.states, self.dones)
            mb_obs.append(np.copy(self.obs))
            mb_actions.append(actions)
            mb_values.append(values)
            mb_dones.append(self.dones)
            obs, rewards, dones, _ = self.env.step(actions)
            self.states = states
            self.dones = dones
            for n, done in enumerate(dones):
                if done:
                    self.obs[n] = self.obs[n]*0
            self.obs = obs
            mb_rewards.append(rewards)
        mb_dones.append(self.dones)
        #batch of steps to batch of rollouts
        mb_obs = np.asarray(mb_obs, dtype=np.uint8).swapaxes(1, 0).reshape(self.batch_ob_shape)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32).swapaxes(1, 0)
        mb_actions = np.asarray(mb_actions, dtype=np.int32).swapaxes(1, 0)
        mb_values = np.asarray(mb_values, dtype=np.float32).swapaxes(1, 0)
        mb_dones = np.asarray(mb_dones, dtype=np.bool).swapaxes(1, 0)
        mb_masks = mb_dones[:, :-1]
        mb_dones = mb_dones[:, 1:]
        last_values = self.model.value(self.obs, self.states, self.dones).tolist()
        #discount/bootstrap off value fn
        for n, (rewards, dones, value) in enumerate(zip(mb_rewards, mb_dones, last_values)):
            rewards = rewards.tolist()
            dones = dones.tolist()
            if dones[-1] == 0:
                rewards = discount_with_dones(rewards+[value], dones+[0], self.gamma)[:-1]
            else:
                rewards = discount_with_dones(rewards, dones, self.gamma)
            mb_rewards[n] = rewards
        mb_rewards = mb_rewards.flatten()
        mb_actions = mb_actions.flatten()
        mb_values = mb_values.flatten()
        mb_masks = mb_masks.flatten()
        return mb_obs, mb_states, mb_rewards, mb_masks, mb_actions, mb_values

def learn(policy, env, seed, nsteps=5, total_timesteps=int(80e6), vf_coef=0.5, ent_coef=0.01, max_grad_norm=0.5, lr=7e-4, lrschedule='linear', epsilon=1e-5, alpha=0.99, gamma=0.99, log_interval=100, save_interval=None, load_snapshot=None, save_timesteps=None):
    tf.reset_default_graph()
    set_global_seeds(seed)

    nenvs = env.num_envs
    ob_space = env.observation_space
    ac_space = env.action_space
    model = Model(policy=policy, ob_space=ob_space, ac_space=ac_space, nenvs=nenvs, nsteps=nsteps, ent_coef=ent_coef, vf_coef=vf_coef,
        max_grad_norm=max_grad_norm, lr=lr, alpha=alpha, epsilon=epsilon, total_timesteps=total_timesteps, lrschedule=lrschedule)
    if load_snapshot:
        model.load(load_snapshot)
        logger.log("Loading snapshot: {}".format(load_snapshot))
    runner = Runner(env, model, nsteps=nsteps, gamma=gamma)

    nbatch = nenvs*nsteps
    # automatically adjust save_timesteps, save more often
    save_timesteps = save_timesteps//nbatch*nbatch
    assert save_timesteps == None or save_timesteps%nbatch == 0, "Save timesteps of {} must align with batch size of {}".format(save_timesteps, nbatch)
    tstart = time.time()
    for update in range(1, total_timesteps//nbatch+1):
        ustart = time.time()
        timesteps = update*nbatch
        obs, states, rewards, masks, actions, values = runner.run()
        policy_loss, value_loss, policy_entropy = model.train(obs, states, rewards, masks, actions, values)
        nseconds = time.time()-tstart
        if update % log_interval == 0 or update == 1:
            useconds = time.time()-ustart
            fps = int((nbatch)/useconds)
            ev = explained_variance(values, rewards)
            logger.record_tabular("nupdates", update)
            logger.record_tabular("total_timesteps", timesteps)
            logger.record_tabular("fps", fps)
            logger.record_tabular("policy_entropy", round(float(policy_entropy),6))
            logger.record_tabular("value_loss", round(float(value_loss),6))
            logger.record_tabular("explained_variance", round(float(ev),6))
            logger.record_tabular("t", round(nseconds,6))
            logger.dump_tabular()

        if save_timesteps and (timesteps%save_timesteps == 0 or timesteps == 1) and logger.get_dir():
            savepath = osp.join(logger.get_dir(), 'snapshot_{0:09d}.pkl'.format(timesteps))
            logger.log("Saving snapshot: {}".format(savepath))
            model.save(savepath)

    env.close()

def eval_policy(policy, env, seed, step_condition, nsteps=5, num_episodes=20, episode_timesteps=int(60*60*5), vf_coef=0.5, ent_coef=0.01, epsilon=0, alpha=0.99, gamma=0, load_snapshot=None, return_runner=False):
    tf.reset_default_graph()
    set_global_seeds(seed)

    nenvs = env.num_envs
    ob_space = env.observation_space
    ac_space = env.action_space
    num_procs = len(env.remotes) # HACK
    model = Model(policy=policy, ob_space=ob_space, ac_space=ac_space, nenvs=nenvs, nsteps=nsteps, ent_coef=ent_coef, vf_coef=vf_coef,
        alpha=alpha, epsilon=epsilon)
    if load_snapshot:
        model.load(load_snapshot)
        logger.log("Loading Snapshot: {}".format(load_snapshot))
    runner = Runner(env, model, nsteps=nsteps, nstack=nstack, gamma=gamma)
    if return_runner:
        # miss doing env.close()
        return runner
    nbatch = nenvs*nsteps
    tstart = time.time()
    while step_condition():
        obs, states, rewards, masks, actions, values = runner.run()
    env.close()

import os
import time
from collections import deque
import pickle

from baselines.asym_ddpg.ddpg import DDPG
import baselines.common.tf_util as U

from baselines import logger
import numpy as np
import tensorflow as tf
from mpi4py import MPI
import cv2
from drive_util import uploadToDrive
PATH = "/tmp/model.ckpt"
def train(env, nb_epochs, nb_epoch_cycles, render_eval, reward_scale, render, param_noise, actor, critic,
    normalize_returns, normalize_observations, normalize_aux, critic_l2_reg, actor_lr, critic_lr, action_noise,
    popart, gamma, clip_norm, nb_train_steps, nb_rollout_steps, nb_eval_steps, batch_size, memory, load_from_file,
    run_name, tau=0.01, eval_env=None, demo_policy=None, num_demo_steps=0, demo_env=None, param_noise_adaption_interval=50, num_pretrain_steps=0):
    rank = MPI.COMM_WORLD.Get_rank()

    assert (np.abs(env.action_space.low) == env.action_space.high).all()  # we assume symmetric actions.
    max_action = env.action_space.high
    logger.info('scaling actions by {} before executing in env'.format(max_action))
    agent = DDPG(actor, critic, memory, env.observation_space.shape, env.action_space.shape, env.state_space.shape, env.aux_space.shape,
        gamma=gamma, tau=tau, normalize_returns=normalize_returns, normalize_observations=normalize_observations,normalize_aux=normalize_aux,
        batch_size=batch_size, action_noise=action_noise, param_noise=param_noise, critic_l2_reg=critic_l2_reg,
        actor_lr=actor_lr, critic_lr=critic_lr, enable_popart=popart, clip_norm=clip_norm,
        reward_scale=reward_scale, run_name=run_name)
    logger.info('Using agent with the following configuration:')
    logger.info(str(agent.__dict__.items()))

    # Set up logging stuff only for a single worker.
    if rank == 0:
        saver = tf.train.Saver()
    else:
        saver = None

    step = 0
    episode = 0
    eval_episode_rewards_history = deque(maxlen=100)
    episode_rewards_history = deque(maxlen=100)
    only_eval = False
    training_text_summary = {
        "env_data": {
            "env:": str(env),
            "run_name": run_name,
            "obs_shape":  env.observation_space.shape,
            "action_shace":  env.action_space.shape,
            "aux_shape":  env.aux_space.shape
        },
        "demo_data": {
            "policy": demo_policy.__class__.__name__,
            "number_of_steps": num_demo_steps,
        },
        "training_data": {
            "nb_train_steps": nb_train_steps,
            "nb_rollout_steps": nb_rollout_steps,
            "num_pretrain_steps": num_pretrain_steps,
            "nb_epochs": nb_epochs,
            "nb_epoch_cycles": nb_epoch_cycles,
        }
    }

    with U.single_threaded_session() as sess:
        # Prepare everything.
        agent.set_sess(sess)
        if not load_from_file:
            agent.initialize()
            print("Model initialized")
            save_path = saver.save(sess, PATH)
            print("Model saved")
        else:
            saver.restore(sess, PATH)
            print("Model restored")
            only_eval = True
        agent.sync_optimizers()
        agent.write_summary(training_text_summary)
        # sess.graph.finalize()

        if eval_env is not None:
            eval_obs = eval_env.reset()
        # TODO HACKERY

        if only_eval:
                for i in range(20):
                    done = False
                    obs = env.reset()
                    agent.reset()
                    total_r = 0
                    while not done:
                        aux0 = env.get_aux()
                        action, q = agent.pi(obs, aux0, apply_noise=False, compute_Q=True)
                        obs, r, done, info = env.step(action)
                        env.render()
                        total_r += r
                    print(total_r)
                return

        if demo_policy:
            _initialize_memory_with_policy(agent, demo_policy, demo_env, num_demo_steps)

        agent.reset()
        obs = env.reset()
        done = False
        episode_reward = 0.
        episode_step = 0
        episodes = 0
        t = 0

        epoch = 0
        start_time = time.time()

        epoch_episode_rewards = []
        epoch_episode_steps = []
        epoch_episode_eval_rewards = []
        epoch_episode_eval_steps = []
        epoch_start_time = time.time()
        epoch_actions = []
        epoch_qs = []
        epoch_episodes = 0





        goal = env.goalstate()
        goal_obs = env.goalobs()
        agent.memory.demonstrationsDone()

        iteration = 0
        while num_pretrain_steps > 0:
            # Adapt param noise, if necessary.
            t+=1
            if len(memory) >= batch_size and t % param_noise_adaption_interval == 0:
                distance = agent.adapt_param_noise()
            cl, al = agent.train(iteration, pretrain=True)
            iteration +=1

            agent.update_target_net()
            num_pretrain_steps -= 1
        eval_episodes = 1

        for epoch in range(nb_epochs):
            for cycle in range(nb_epoch_cycles):
                print ("Cycle: {}/{}".format(cycle, nb_epoch_cycles) +
                       "["+ "-" * cycle + " " * (nb_epoch_cycles - cycle) + "]" 
                 , end="\r")
                # Perform rollouts.
                for t_rollout in range(nb_rollout_steps):
                    # Predict next action.
                    aux0 = env.get_aux()
                    action, q = agent.pi(obs, aux0, apply_noise=True, compute_Q=True)
                    assert action.shape == env.action_space.shape

                    # Execute next action.
                    if rank == 0 and render:
                        print("a")
                        env.render()
                    assert max_action.shape == action.shape
                    state =  env.get_state()
                    new_obs, r, done, info = env.step(max_action * action)  # scale for execution in env (as far as DDPG is concerned, every action is in [-1, 1])
                    t += 1
                    if rank == 0 and render:
                        print("a")

                        env.render()
                    episode_reward += r
                    episode_step += 1

                    # Book-keeping.
                    epoch_actions.append(action)
                    epoch_qs.append(q)


                    state1 = env.get_state()
                    aux1 = env.get_aux()



                    agent.store_transition(state, obs, action, r, state1, new_obs, done, goal, goal_obs, aux0, aux1)
                    obs = new_obs

                    if done:
                        # Episode done.
                        agent.save_reward(episode_reward, episodes)
                        epoch_episode_rewards.append(episode_reward)
                        episode_rewards_history.append(episode_reward)
                        epoch_episode_steps.append(episode_step)
                        episode_reward = 0.
                        episode_step = 0
                        epoch_episodes += 1
                        episodes += 1

                        agent.reset()
                        obs = env.reset()

                        goal = env.goalstate()
                        goal_obs = env.goalobs()

                # Train.
                epoch_actor_losses = []
                epoch_critic_losses = []
                epoch_adaptive_distances = []
                for t_train in range(nb_train_steps):
                    # Adapt param noise, if necessary.
                    if memory.nb_entries >= batch_size and t_train % param_noise_adaption_interval == 0:
                        distance = agent.adapt_param_noise()
                        epoch_adaptive_distances.append(distance)
                    cl, al = agent.train(iteration)
                    iteration += 1
                    epoch_critic_losses.append(cl)
                    epoch_actor_losses.append(al)
                    agent.update_target_net()

                # Evaluate.
                eval_episode_rewards = []
                eval_qs = []
                if eval_env is not None and cycle == 0:
                    eval_episode_reward = 0.
                    if render_eval:
                        fname= '/tmp/jm6214/ddpg/eval-{}-{}.avi'.format(run_name, epoch + 1)
                        fourcc = cv2.VideoWriter_fourcc(*"XVID")
                        rgb = cv2.VideoWriter(fname, fourcc, 30.0, (84, 84))
                    for t_rollout in range(nb_eval_steps):
                        aux = eval_env.get_aux()
                        eval_action, eval_q = agent.pi(eval_obs, aux, apply_noise=False, compute_Q=True)
                        eval_obs, eval_r, eval_done, eval_info = eval_env.step(max_action * eval_action)  # scale for execution in env (as far as DDPG is concerned, every action is in [-1, 1])
                        if render_eval:
                            frame = np.array(eval_obs[:,:,0:3].copy()*255, dtype=np.uint8)
                            cv2.putText(frame,format(eval_r, '.2f'), (40,15), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255,0,0), 1)
                            rgb.write(frame)
                        eval_episode_reward += eval_r

                        eval_qs.append(eval_q)
                        if eval_done:
                            eval_obs = eval_env.reset()
                            eval_episode_rewards.append(eval_episode_reward)
                            eval_episode_rewards_history.append(eval_episode_reward)
                            agent.save_eval_reward(eval_episode_reward, eval_episodes)
                            eval_episodes += 1
                            eval_episode_reward = 0.
                    if render_eval:
                        rgb.release()
                        uploadToDrive(run_name, "epoch_{}.avi".format(epoch+1), fname, delete=True)
                        print("Uploaded video to drive.")

            mpi_size = MPI.COMM_WORLD.Get_size()
            # Log stats.
            # XXX shouldn't call np.mean on variable length lists
            duration = time.time() - start_time
            stats = agent.get_stats()
            combined_stats = stats.copy()
            combined_stats['rollout/return'] = np.mean(epoch_episode_rewards)
            combined_stats['rollout/return_history'] = np.mean(episode_rewards_history)
            combined_stats['rollout/episode_steps'] = np.mean(epoch_episode_steps)
            combined_stats['rollout/actions_mean'] = np.mean(epoch_actions)
            combined_stats['rollout/Q_mean'] = np.mean(epoch_qs)
            combined_stats['train/loss_actor'] = np.mean(epoch_actor_losses)
            combined_stats['train/loss_critic'] = np.mean(epoch_critic_losses)
            combined_stats['train/param_noise_distance'] = np.mean(epoch_adaptive_distances)
            combined_stats['total/duration'] = duration
            combined_stats['total/steps_per_second'] = float(t) / float(duration)
            combined_stats['total/episodes'] = episodes
            combined_stats['rollout/episodes'] = epoch_episodes
            combined_stats['rollout/actions_std'] = np.std(epoch_actions)
            # Evaluation statistics.
            if eval_env is not None:
                combined_stats['eval/return'] = np.mean(eval_episode_rewards)
                combined_stats['eval/return_history'] = np.mean(eval_episode_rewards_history)
                combined_stats['eval/Q_mean'] =  np.mean(eval_qs)
                combined_stats['eval/episodes'] = len(eval_episode_rewards)
            print (combined_stats)
            def as_scalar(x):
                if isinstance(x, list):
                    assert len(x) == 1
                    return x[0]
                if isinstance(x, np.ndarray):
                    assert x.size == 1
                    return x[0]
                elif np.isscalar(x):
                    return x
                else:
                    raise ValueError('expected scalar, got %s'%x)
            combined_stats_sums = MPI.COMM_WORLD.allreduce(np.array([as_scalar(x) for x in combined_stats.values()]))
            combined_stats = {k : v / mpi_size for (k,v) in zip(combined_stats.keys(), combined_stats_sums)}

            # Total statistics.
            combined_stats['total/epochs'] = epoch + 1
            combined_stats['total/steps'] = t

            for key in sorted(combined_stats.keys()):
                logger.record_tabular(key, combined_stats[key])
            logger.dump_tabular()
            logger.info('')
            logdir = logger.get_dir()
            if rank == 0 and logdir:
                if hasattr(env, 'get_state'):
                    with open(os.path.join(logdir, 'env_state.pkl'), 'wb') as f:
                        pickle.dump(env.get_state(), f)
                if eval_env and hasattr(eval_env, 'get_state'):
                    with open(os.path.join(logdir, 'eval_env_state.pkl'), 'wb') as f:
                        pickle.dump(eval_env.get_state(), f)
            save_path = saver.save(sess, PATH)
            print("Model saved")


def _initialize_memory_with_policy(agent, demo_policy, demo_env, num_demo_steps):
    print("Start collecting demo transitions")
    obs0 = demo_env.reset()
    demo_policy.reset()
    goal = demo_env.goalstate()
    goal_obs = demo_env.goalobs()
    for i in range(num_demo_steps):
        aux0 = demo_env.get_aux()
        state0 = demo_env.get_state()
        action = demo_policy.choose_action(state0)
        obs1, r, done, info = demo_env.step(action)
        aux1 = demo_env.get_aux()
        state1 = demo_env.get_state()
        agent.store_transition(state0, obs0, action, r, state1, obs1, done, goal, goal_obs, aux0, aux1, demo=True)
        obs0 = obs1
        if done:
            obs0 = demo_env.reset()
            demo_policy.reset()
            goal = demo_env.goalstate()
            goal_obs = demo_env.goalobs()
    print("Collected {} demo transition.".format(agent.memory._num_demonstrations))

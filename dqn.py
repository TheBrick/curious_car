import argparse
import random

import gym
import numpy as np
import torch
import torch.nn.functional as F
from torch import optim
from tqdm import tqdm as _tqdm

import plotting
import utils
from ReplayMemory import ReplayMemory
from defaults import Config
from models import StatePredictor, QNetwork
from utils import render_environment
import os
import csv


def tqdm(*args, **kwargs):
    # Safety, do not overflow buffer
    return _tqdm(*args, **kwargs,
                 mininterval=1)


env = gym.envs.make("MountainCar-v0")


def select_action(model, state, epsilon):
    with torch.no_grad():
        state = torch.tensor(state.astype(np.float32), device=model.device)

        # compute action values
        action_values = model(state)

        # determine greedy and random action
        prob_a, greedy_a = action_values.max(dim=0)
        greedy_a = greedy_a.item()

        # determine action to choose based on eps
        if random.random() < epsilon:
            return random.choice([0, 1, 2])

        return greedy_a


def get_epsilon(it):
    if it > 1000:
        return 0.05
    return 1 - it * (0.95 / 1000)


def compute_q_val(model, state, action):
    output = model(torch.tensor(state, dtype=torch.float, device=model.device))
    return output[np.arange(output.size(0)), action]


def compute_target(model, reward, next_state, done, discount_factor):
    # Done is a boolean (vector) that indicates if next_state is terminal.

    output = model(next_state)
    q_values, _ = output.max(dim=1)

    # Multiply q_values with 0 for terminal states
    return reward + discount_factor * (done == False).type(
        torch.float) * q_values


def train(model, memory, optimizer, config: Config):
    # Don't learn without some decent experience.
    if len(memory) < config.batch_size:
        return None

    # Random transition batch is taken from experience replay memory.
    transitions = memory.sample(config.batch_size)

    # Transition is a list of 4-tuples, instead we want 4 torch.Tensor's.
    state, action, reward, next_state, done = zip(*transitions)

    # Convert to PyTorch and define types
    state = torch.tensor(state, dtype=torch.float, device=model.device)
    action = torch.tensor(action, dtype=torch.int64, device=model.device)
    next_state = torch.tensor(next_state, dtype=torch.float,
                              device=model.device)
    reward = torch.tensor(reward, dtype=torch.float, device=model.device)
    done = torch.tensor(done, dtype=torch.uint8, device=model.device)

    if isinstance(model, QNetwork):
        # compute the q value
        q_val = compute_q_val(model, state, action)

        with torch.no_grad():
            # Don't compute gradient info for the target (semi-gradient).
            target = compute_target(model, reward, next_state, done,
                                    config.discount_factor)

        # Loss is measured from error between current and new Q values.
        loss = F.smooth_l1_loss(q_val, target)
    elif isinstance(model, StatePredictor):

        prediction = model(state, action)
        loss = F.mse_loss(prediction, next_state.to(model.device))

    optimizer.zero_grad()
    loss.backward()
    optimizer.step()

    return loss.item()


def run_episodes(train, q_model, curiosity_model, memory, env, experiment_seed, config: Config, experiment_number):
    # adjust hyperparameters per experiment:
    lrs_q_model = [1e-6,5e-6,8e-6,1e-5,1e-6,5e-7,1e-7,5e-8]
    nums_hidden_q_model = [50,50,50,50,30,30,30,30,30]
    config.lr_q_model = lrs_q_model[experiment_number-20]
    config.num_hidden_q_model = nums_hidden_q_model[experiment_number-20]






    optimizer = optim.Adam(
        [{'params': q_model.parameters(),
          'lr': config.lr_q_model},
         {'params': curiosity_model.parameters(),
          'lr': config.lr_curiosity_model}])

    # Count the steps (do not reset at episode start, to compute epsilon)
    global_steps = 0
    all_metrics = []
    episode_durations = []  #
    losses = []
    for i in range(config.num_episodes):

        episode_seed = experiment_seed + i
        env.seed(episode_seed)
        random.seed(episode_seed)

        # initialize episode
        done = False
        state = env.reset()
        ep_length = 0
        max_x = state[0]
        extrinsic_rewards = []
        intrinsic_rewards = []

        # save action for rendering
        actions = []
        start = state

        # keep acting until terminal state is reached
        while not done:
            # calculate next action
            epsilon = get_epsilon(global_steps)
            action = select_action(q_model, state, epsilon)

            actions.append(action)

            # perform action
            next_state, extrinsic_reward, done, _ = env.step(action)

            # calculate intrinsic reward
            with torch.no_grad():
                state_tensor = torch.tensor([state], dtype=torch.float,
                                            device=curiosity_model.device)
                action = torch.tensor([action],
                                      device=curiosity_model.device)
                pred = curiosity_model(state_tensor, action)
                intrinsic_reward = F.mse_loss(pred, torch.tensor([next_state],
                                                       dtype=torch.float,
                                                       device=curiosity_model.device))
                intrinsic_reward = intrinsic_reward.item()

            # Save metrics for later
            extrinsic_rewards.append(extrinsic_reward)
            intrinsic_rewards.append(intrinsic_reward)

            if config.curious:
                reward = intrinsic_reward
            else:
                reward = extrinsic_reward

            # remember transition
            memory.push((state, action, reward, next_state, done))
            state = next_state

            ep_length += 1
            global_steps += 1
            max_x = max(max_x, state[0])

            # update model
            q_loss = train(q_model, memory, optimizer, config)
            losses.append(q_loss)

            curiosity_loss = train(curiosity_model, memory, optimizer,
                                   config)

        # Finished the episode

        # Save metrics for this episode
        episode_metrics = {}
        episode_metrics['episode'] = i
        episode_metrics['target_reward'] = 'intrinsic' if config.curious else 'extrinsic'
        episode_metrics['episode_seed'] = episode_seed
        episode_metrics['episode_length'] = ep_length
        episode_metrics['total_extrinsic_reward'] = np.sum(extrinsic_rewards)
        episode_metrics['total_intrinsic_reward'] = np.sum(intrinsic_rewards)
        episode_metrics['min_extrinsic_reward'] = np.min(intrinsic_rewards)
        episode_metrics['median_extrinsic_reward'] = np.median(intrinsic_rewards)
        episode_metrics['max_extrinsic_reward'] = np.max(intrinsic_rewards)
        episode_metrics['max_x'] = max_x
        all_metrics.append(episode_metrics)

        print(episode_seed, ep_length, max_x)
        #plotting.visualize_policy(q_model)
        if ep_length < 200:
            if config.render:
                render_environment(env, start, actions, i)
            utils.save_check_point(q_model, curiosity_model, config,
                                   episode=i, max_x=max_x)

        episode_durations.append(ep_length)
        utils.save_check_point(q_model, curiosity_model, config,
                               episode=i, max_x=max_x)

    # Finished all the episodes

    if config.save_to_disk:
        # Save metrics to disk
        # Create folder, named by the seed
        folder = "experiments/"+str(experiment_number)
        if not os.path.exists(folder):
            os.makedirs(folder)
            with open(folder+'/test_configs.txt','w') as f1:
                f1.write('lrs_q_model: '+ str(config.lr_q_model))
                f1.write('\n nums_hidden_q_model'+ str(config.num_hidden_q_model))

        # Export CSV file with all metrics for each episode
        filename = "{}/metrics_{}_{}.csv".format(folder, "curious" if config.curious else "noncurious", experiment_seed)
        with open(filename, 'w') as f:
            w = csv.DictWriter(f, all_metrics[0].keys())
            w.writeheader()
            w.writerows(all_metrics)

        path = folder + '/'
        plotting.plot_experiment(path, (config.lr_q_model,config.num_hidden_q_model))

    return episode_durations, losses


def main(config: Config):
    print(config)

    # Let's run it!
    for i in range(config.num_experiments):
        experiment_seed = config.seed + i * config.num_episodes
        memory = ReplayMemory(config.replay_memory_size)

        # We will seed the algorithm (for reproducability).
        random.seed(experiment_seed)
        torch.manual_seed(experiment_seed)
        env.seed(experiment_seed)

        q_model = QNetwork(config.device, config.num_hidden_q_model)
        curiousity_model = StatePredictor(2, 3,
                                          config.num_hidden_curiosity_model,
                                          config.device)

        for i in range(20,29):
            episode_durations, episode_loss = run_episodes(train, q_model,
                                                       curiousity_model, memory,
                                                       env, experiment_seed, config, experiment_number = i)
        # print(i, episode_durations, episode_loss)
        print("Finished experiment {}/{}".format(i+1, config.num_experiments))

# this thing is necessary because argparse cannot deal
# with explicitly setting boolean flags to false
def str_to_bool(value):
    if isinstance(value, bool):
        return value
    if value.lower() in {'false', 'f', '0', 'no', 'n'}:
        return False
    elif value.lower() in {'true', 't', '1', 'yes', 'y'}:
        return True
    raise ValueError(f'{value} is not a valid boolean value')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--device', type=str, default=Config.device,
                        help="Training device 'cpu' or 'cuda:0'")
    parser.add_argument('--num_experiments', type=int, default=Config.num_experiments)
    parser.add_argument('--num_episodes', type=int,
                        default=Config.num_episodes)
    parser.add_argument('--batch_size', type=int, default=Config.batch_size)
    parser.add_argument('--discount_factor', type=float,
                        default=Config.discount_factor)
    parser.add_argument('--lr_curiosity_model', type=float,
                        default=Config.lr_curiosity_model)
    parser.add_argument('--lr_q_model', type=float, default=Config.lr_q_model)
    parser.add_argument('--replay_memory_size', type=int,
                        default=Config.replay_memory_size)
    parser.add_argument('--seed', type=int, default=Config.seed)
    parser.add_argument('--num_hidden_q_model', type=int,
                        default=Config.num_hidden_q_model)
    parser.add_argument('--num_hidden_curiosity_model', type=int,
                        default=Config.num_hidden_curiosity_model)
    parser.add_argument('--render', type=str_to_bool, default=Config.render, nargs='?', const=True)
    parser.add_argument('--curious', type=str_to_bool, default=Config.curious, nargs='?', const=True)
    parser.add_argument('--save_to_disk', type=str_to_bool, default=Config.save_to_disk, nargs='?', const=True)
    config: Config = parser.parse_args()

    main(config)

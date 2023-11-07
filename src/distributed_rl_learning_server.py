#!/usr/bin/env python

from __future__ import annotations

import argparse
import os
import sys
import random
import math
import re
from glob import glob

from threading import Thread, Lock, Event
from pathlib import Path
from typing import List, Set, Optional, Dict, Tuple, Sequence

print("Finished std imports", file=sys.stderr)

import torch
print("Finished main torch import", file=sys.stderr)
from torch import nn
print("Finished nn import", file=sys.stderr)
import torch.nn.functional as F
print("Finished functional import", file=sys.stderr)
from torch import optim
print("Finished optim import", file=sys.stderr)
import torch.distributed as dist

print("Finished torch imports", file=sys.stderr)
# pylint: disable=wrong-import-position
sys.path.append(str(Path(os.getcwd()) / "src"))
from rl import model_setup, optimizers, ReplayBuffer
print("Imported rl model setup", file=sys.stderr)
from util import eprint, unwrap, print_time
# pylint: enable=wrong-import-position
eprint("Finished imports")


def main() -> None:
  eprint("Starting main")
  parser = argparse.ArgumentParser()
  parser.add_argument("--state-dir", type=Path, default="drl_state")
  parser.add_argument("-e", "--encoding-size", type=int, required=True)
  parser.add_argument("-l", "--learning-rate", default=5e-6, type=float)
  parser.add_argument("-b", "--batch-size", default=64, type=int)
  parser.add_argument("-g", "--gamma", default=0.9, type=float)
  parser.add_argument("--hidden-size", type=int, default=128)
  parser.add_argument("--num-layers", type=int, default=3)
  parser.add_argument("--allow-partial-batches", action='store_true')
  parser.add_argument("--window-size", type=int, default=2560)
  parser.add_argument("--train-every", type=int, default=8)
  parser.add_argument("--sync-target-every", type=int, default=32)
  parser.add_argument("--keep-latest", default=3, type=int)
  parser.add_argument("--sync-workers-every", type=int, default=16)
  parser.add_argument("--optimizer", choices=optimizers.keys(), default=list(optimizers.keys())[0])
  parser.add_argument("--verifyv-every", type=int, default=None)
  parser.add_argument("--start-from", type=Path, default=None)
  parser.add_argument("--ignore-after", type=int, default=None)
  args = parser.parse_args()

  with (args.state_dir / "learner_scheduled.txt").open('w') as f:
      print("1", file=f, flush=True)
  serve_parameters(args)

def serve_parameters(args: argparse.Namespace, backend='mpi') -> None:
  eprint("Establishing connection")
  dist.init_process_group(backend)
  eprint("Connection established")
  assert torch.cuda.is_available(), "Training node doesn't have CUDA available!"
  device = "cuda"
  v_network: nn.Module = model_setup(args.encoding_size, args.hidden_size,
                                     args.num_layers).to(device)
  if args.start_from is not None:
    _, _, _, network_state, \
      tnetwork_state, shorter_proofs_dict, _ = \
        torch.load(str(args.start_from), map_location=device)
    eprint(f"Loading initial weights from {args.start_from}")
    inner_network_state, _encoder_state, _obl_cache = network_state
    v_network.load_state_dict(inner_network_state)
  target_network: nn.Module = model_setup(args.encoding_size,
                                          args.hidden_size,
                                          args.num_layers).to(device)
  target_network.load_state_dict(v_network.state_dict())
  optimizer: optim.Optimizer = optimizers[args.optimizer](v_network.parameters(),
                                                          lr=args.learning_rate)
  verification_states: Dict[torch.FloatTensor, int] = {}
  replay_buffer = EncodedReplayBuffer(args.window_size,
                                      args.allow_partial_batches)
  signal_change = Event()
  buffer_thread = BufferPopulatingThread(replay_buffer, verification_states,
                                         signal_change, args.encoding_size,
                                         args.ignore_after)
  buffer_thread.start()

  steps_last_trained = 0
  steps_last_synced_target = 0
  steps_last_synced_workers = 0
  common_network_version = 0
  iters_trained = 0
  last_iter_verified = 0

  while signal_change.wait():
    signal_change.clear()
    if replay_buffer.buffer_steps - steps_last_trained >= args.train_every:
      steps_last_trained = replay_buffer.buffer_steps
      train(args, v_network, target_network, optimizer, replay_buffer)
      iters_trained += 1
    if replay_buffer.buffer_steps - steps_last_synced_target >= args.sync_target_every:
      eprint(f"Syncing target network at step {replay_buffer.buffer_steps} ({replay_buffer.buffer_steps - steps_last_synced_target} steps since last synced)")
      steps_last_synced_target = replay_buffer.buffer_steps
      if replay_buffer.buffer_steps > args.ignore_after:
          eprint("Skipping sync because we're ignoring samples now")
      else:
          target_network.load_state_dict(v_network.state_dict())
    if replay_buffer.buffer_steps - steps_last_synced_workers >= args.sync_workers_every:
      steps_last_synced_workers = replay_buffer.buffer_steps
      send_new_weights(args, v_network, common_network_version)
      common_network_version += 1
    if args.verifyv_every is not None and \
       iters_trained - last_iter_verified >= args.verifyv_every:
      print_vvalue_errors(args.gamma, v_network, verification_states)
      last_iter_verified = iters_trained

def train(args: argparse.Namespace, v_model: nn.Module,
          target_model: nn.Module,
          optimizer: optim.Optimizer,
          replay_buffer: EncodedReplayBuffer) -> None:
  samples = replay_buffer.sample(args.batch_size)
  if samples is None:
    return
  eprint(f"Got {len(samples)} samples to train")
  inputs = torch.cat([start_obl.view(1, args.encoding_size)
                      for start_obl, _action_records in samples], dim=0)
  num_resulting_obls = [[len(resulting_obls)
                         for _action, resulting_obls in action_records]
                        for _start_obl, action_records in samples]

  all_resulting_obls = [obl for _start_obl, action_records in samples
                        for _action, resulting_obls in action_records
                        for obl in resulting_obls]
  if len(all_resulting_obls) > 0:
    with torch.no_grad():
      all_obls_tensor = torch.cat([obl.view(1, args.encoding_size) for obl in
                                   all_resulting_obls], dim=0)
      eprint(all_obls_tensor)
      all_obl_scores = target_model(all_obls_tensor)
  else:
      all_obl_scores = []
  outputs = []
  cur_row = 0
  for resulting_obl_lens in num_resulting_obls:
    action_outputs = []
    for num_obls in resulting_obl_lens:
      selected_obl_scores = all_obl_scores[cur_row:cur_row+num_obls]
      action_outputs.append(args.gamma * math.prod(selected_obl_scores))
      cur_row += num_obls
    outputs.append(max(action_outputs))
  actual_values = v_model(inputs).view(len(samples))
  device = "cuda"
  target_values = torch.FloatTensor(outputs).to(device)
  loss = F.mse_loss(actual_values, target_values)
  eprint(f"Loss: {loss}")
  optimizer.zero_grad()
  loss.backward()
  optimizer.step()

class BufferPopulatingThread(Thread):
  replay_buffer: EncodedReplayBuffer
  verification_states: Dict[torch.FloatTensor, int]
  signal_change: Event
  ignore_after: Optional[int]
  def __init__(self, replay_buffer: EncodedReplayBuffer,
               verification_states: Dict[torch.FloatTensor, int],
               signal_change: Event, encoding_size: int, ignore_after: Optional[int] = None) -> None:
    self.replay_buffer = replay_buffer
    self.signal_change = signal_change
    self.encoding_size = encoding_size
    self.verification_states = verification_states
    self.ignore_after = ignore_after
    super().__init__()
    pass
  def run(self) -> None:
    while True:
      send_type = torch.zeros(1, dtype=int)
      sending_worker = dist.recv(tensor=send_type, tag=4)
      if send_type.item() == 0:
        self.receive_experience_sample(sending_worker)
      elif send_type.item() == 1:
        self.receive_verification_sample(sending_worker)

  def receive_experience_sample(self, sending_worker: int) -> None:
    if self.replay_buffer.buffer_steps >= self.ignore_after:
        eprint("Ignoring a sample, but training anyway")
        self.replay_buffer.buffer_steps += 1
        self.signal_change.set()
        return
    device = "cuda"
    newest_prestate_sample: torch.FloatTensor = \
      torch.zeros(self.encoding_size, dtype=torch.float32) #type: ignore
    dist.recv(tensor=newest_prestate_sample, src=sending_worker, tag=0)
    newest_action_sample = torch.zeros(1, dtype=int)
    dist.recv(tensor=newest_action_sample, src=sending_worker, tag=1)
    number_of_poststates = torch.zeros(1, dtype=int)
    dist.recv(tensor=number_of_poststates, src=sending_worker, tag=2)
    post_states = []
    for _ in range(number_of_poststates.item()):
      newest_poststate_sample: torch.FloatTensor = \
        torch.zeros(self.encoding_size, dtype=torch.float32) #type: ignore
      dist.recv(tensor=newest_poststate_sample, src=sending_worker, tag=3)
      post_states.append(newest_poststate_sample.to(device))
    self.replay_buffer.add_transition(
      (newest_prestate_sample.to(device), int(newest_action_sample.item()),
       post_states))
    self.replay_buffer.buffer_steps += 1
    self.signal_change.set()

  def receive_verification_sample(self, sending_worker: int) -> None:
    device = "cuda"
    state_sample: torch.FloatTensor = \
      torch.zeros(self.encoding_size, dtype=torch.float32)  #type: ignore
    dist.recv(tensor=state_sample, src=sending_worker, tag=5)
    target_steps: torch.LongTensor = torch.zeros(1, dtype=int) #type: ignore
    dist.recv(tensor=target_steps, src=sending_worker, tag=6)
    state_sample = state_sample.to(device)
    if state_sample in self.verification_states:
      assert target_steps.item() <= self.verification_states[state_sample], \
        "Got sent a target value  less than the previously expected value for this state!"
    self.verification_states[state_sample] = target_steps.item()

EObligation = torch.FloatTensor
ETransition = Tuple[int, Sequence[EObligation]]
EFullTransition = Tuple[EObligation, int, List[EObligation]]
class EncodedReplayBuffer:
  buffer_steps: int
  lock: Lock
  _contents: Dict[EObligation, Tuple[int, Set[ETransition]]]
  window_size: int
  window_end_position: int
  allow_partial_batches: bool
  def __init__(self, window_size: int,
               allow_partial_batches: bool) -> None:
    self.window_size = window_size
    self.window_end_position = 0
    self.allow_partial_batches = allow_partial_batches
    self._contents = {}
    self.lock = Lock()
    self.buffer_steps = 0

  def sample(self, batch_size: int) -> \
        Optional[List[Tuple[EObligation, Set[ETransition]]]]:
    with self.lock:
      sample_pool: List[Tuple[EObligation, Set[ETransition]]] = []
      for obl, (last_updated, transitions) in self._contents.copy().items():
        if last_updated <= self.window_end_position - self.window_size:
          del self._contents[obl]
        else:
          sample_pool.append((obl, transitions))
      if len(sample_pool) >= batch_size:
        return random.sample(sample_pool, batch_size)
      if self.allow_partial_batches and len(sample_pool) > 0:
        return sample_pool
      return None

  def add_transition(self, transition: EFullTransition) -> None:
    with self.lock:
      from_obl, action, _ = transition
      to_obls = tuple(transition[2])
      self._contents[from_obl] = \
        (self.window_end_position,
         {(action, to_obls)} |
         self._contents.get(from_obl, (0, set()))[1])
      self.window_end_position += 1

def send_new_weights(args: argparse.Namespace, v_network: nn.Module, version: int) -> None:
  save_path = str(args.state_dir / "weights" / f"common-v-network-{version}.dat")
  torch.save(v_network.state_dict(), save_path + ".tmp")
  os.rename(save_path + ".tmp", save_path)
  delete_old_common_weights(args)

def delete_old_common_weights(args: argparse.Namespace) -> None:
  cwd = os.getcwd()
  root_dir = str(args.state_dir / "weights")
  os.chdir(root_dir)
  common_network_paths = glob("common-v-network-*.dat")
  os.chdir(cwd)

  common_save_nums = [int(unwrap(re.match(r"common-v-network-(\d+).dat", path)).group(1))
                      for path in common_network_paths]
  latest_common_save_num = max(common_save_nums)
  for save_num in common_save_nums:
    if save_num > latest_common_save_num - args.keep_latest:
      continue
    old_save_path = (args.state_dir / "weights" /
                     f"common-v-network-{save_num}.dat")
    old_save_path.unlink()

def print_vvalue_errors(gamma: float, vnetwork: nn.Module,
                        verification_samples: Dict[torch.FloatTensor, int]):
  device = "cuda"
  items = list(verification_samples.items())
  predicted_v_values = vnetwork(torch.cat([obl.view(1, -1) for obl, _ in items], dim=0)).view(-1)
  predicted_steps = torch.log(predicted_v_values) / math.log(gamma)
  target_steps: FloatTensor = torch.tensor([steps for _, steps in items]).to(device) #type: ignore
  step_errors = torch.abs(predicted_steps - target_steps)
  total_error = torch.sum(step_errors).item()
  avg_error = total_error / len(items)
  eprint(f"Average V Value error across {len(items)} initial states: {avg_error:.6f}")

if __name__ == "__main__":
  main()

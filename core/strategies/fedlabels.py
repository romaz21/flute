# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import json
import logging
import os

import torch
import numpy as np
from azureml.core import Run

from core.strategies import BaseStrategy
from utils import (
    compute_grad_cosines, 
    print_rank, 
    to_device)

run = Run.get_context()

class FedLabels(BaseStrategy):
    '''FedLabels: Semi-supervision strategy.'''

    def __init__(self, mode, config, model_path=None):
        '''
        Args:
            mode (str): which part the instantiated object should play,
                typically either :code:`client` or :code:`server`.
            config (dict): initial config dict.
            model_path (str): where to find model, needed for debugging only.
        '''

        super().__init__(mode=mode, config=config, model_path=model_path)

        if mode not in ['client', 'server']:
            raise ValueError('mode in strategy must be either `client` or `server`')

        self.config = config
        self.model_path = model_path
        self.mode = mode
        self.model_config = config['model_config']
        self.client_config = config['client_config']
        self.server_config = config['server_config']
        self.dp_config = config.get('dp_config', None)

        self.tmp_sup = None
        self.tmp_unsup = None

        if mode == 'client':
            self.stats_on_smooth_grad = self.client_config.get('stats_on_smooth_grad', False)
        elif mode == 'server':
            self.dump_norm_stats = self.config.get('dump_norm_stats', False)
            self.aggregate_fast = self.server_config.get('fast_aggregation', False)

            self.skip_model_update = False

            # Initialize accumulators
            self.client_parameters_stack = []
            self.client_weights = []

    def generate_client_payload(self, trainer):
        '''Generate client payload

        Args:
            trainer (core.Trainer object): trainer on client.
            unsup_dict (dict): unsupervised model state dictionary
            iteration (int): training round
            total_est_labels (int): labels generated

        Returns:
            dict containing payloads in some specified format.
        '''

        unsup_dict = trainer.algo_computation

        if self.mode != 'client':
            raise RuntimeError('this method can only be invoked by the client')

        # Reset gradient stats and recalculate them on the smooth/pseudo gradient
        if self.stats_on_smooth_grad:
            trainer.reset_gradient_power()
            trainer.estimate_sufficient_stats()

        # Weight the gradient and preprocess state dictionaries from supervised and unsupervised model
        weight = 1 if trainer.num_samples == 0 else trainer.num_samples
        unsup_grads = [unsup_dict[param_tensor].to(torch.device('cpu')) for param_tensor in unsup_dict.keys()]
        sup_grads = [trainer.model.state_dict()[param_tensor].to(torch.device('cpu')) for param_tensor in trainer.model.state_dict().keys()]

        payload = {}
        payload['weight'] = weight
        payload['gradients'] = sup_grads + unsup_grads

        return payload

    def process_individual_payload(self, worker_trainer, payload):
        '''Process client payload

        Args:
            worker_trainer (core.Trainer object): trainer on server
                (aka model updater).
            payload (dict): whatever is generated by
                :code:`generate_client_payload`.

        Returns:
            True if processed succesfully, False otherwise.
        '''

        if self.mode != 'server':
            raise RuntimeError('this method can only be invoked by the server')

        if payload['weight'] == 0.0:
            return False

        self.client_weights.append(payload['weight'])
        if self.aggregate_fast:
            aggregate_gradients_inplace(worker_trainer.model, payload['gradients'])
        else:
            self.client_parameters_stack.append(payload['gradients'])
        return True

    def combine_payloads(self, worker_trainer, curr_iter, num_clients_curr_iter, total_clients, client_stats, logger=None):
        '''Combine payloads to update model

        Args:
            worker_trainer (core.Trainer object): trainer on server
                (aka model updater).
            curr_iter (int): current iteration.
            num_clients_curr_iter (int): number of clients on current iteration.
            client_stats (dict): stats being collected.
            logger (callback): function called to log quantities.

        Returns:
            losses, computed for use with LR scheduler.
        '''

        if self.mode != 'server':
            raise RuntimeError('this method can only be invoked by the server')

        # Aggregation step
        if self.dump_norm_stats:
            cps_copy = [[g.clone().detach() for g in x] for x in self.client_parameters_stack]
        weight_sum, self.tmp_sup, self.tmp_unsup = self._aggregate_gradients(worker_trainer, num_clients_curr_iter, self.client_weights, metric_logger=logger)
        print_rank('Sum of weights: {}'.format(weight_sum), loglevel=logging.DEBUG)
        torch.cuda.empty_cache()

        # Disjoint aggregation
        tmp_both = {}
        for param_key in self.tmp_unsup.keys():
                tmp_both[param_key] = self.tmp_sup[param_key]/2 + self.tmp_unsup[param_key]/2
        worker_trainer.model.load_state_dict(tmp_both)
        
        if self.dump_norm_stats:
            cosines = compute_grad_cosines(cps_copy, [p.grad.clone().detach() for p in worker_trainer.model.parameters()])
            with open(os.path.join(self.model_path, 'cosines.txt'), 'a', encoding='utf-8') as outfile:
                outfile.write('{}\n'.format(json.dumps(cosines)))

        if self.skip_model_update is True:
            print_rank('Skipping model update')
            return

        # Run optimization with gradient/model aggregated from clients
        print_rank('Updating model')
        worker_trainer.update_model()
        print_rank('Updating learning rate scheduler')
        losses = worker_trainer.run_lr_scheduler(force_run_val=False)

        # TODO: Global DP. See dga.py

        return losses

    def _aggregate_gradients(self, worker_trainer, num_clients_curr_iter, client_weights, metric_logger=None):
        '''Go through stored gradients, aggregate and put them inside model.

        Args:
            num_clients_curr_iter (int): how many clients were processed.
            client_weights: weight for each client.
            metric_logger (callback, optional): callback used for logging.
                Defaults to None, in which case AML logger is used.

        Returns:
            float: sum of weights for all clients.
            dict: supervised model state dictionary.
            dict: unsupervised model state dicionary.
        '''

        if metric_logger is None:
            metric_logger = run.log

        # Separate sup/unsup dictionaries from client payload
        sup_slice = int(len(self.client_parameters_stack[0])/2)
        keys = [key for key in worker_trainer.model.state_dict()]
        model_dicts = [client_dict[:sup_slice] for client_dict in self.client_parameters_stack]
        unsup_dicts = [client_dict[sup_slice:] for client_dict in self.client_parameters_stack]

        first = True
        tmp_sup, tmp_unsup = {}, {}

        # Compute radios for each model
        weight_sum = sum(client_weights)
        ratio_sup = 1/len(client_weights)
        ratio_unsup = np.array(client_weights)/weight_sum

        if not self.aggregate_fast:
            # Perform aggregation for supervised model
            for i, client_parameters in enumerate(model_dicts):
                first, tmp_sup = aggregate_gradients_inplace(keys, client_parameters, first, tmp_sup, ratio_sup)
            first = True
            
            # Perform aggregation for unsupervised model
            for j, client_parameters in enumerate(unsup_dicts):
                first, tmp_unsup = aggregate_gradients_inplace(keys, client_parameters, first, tmp_unsup, ratio_unsup[j])
        
        # Some cleaning
        self.client_parameters_stack = []
        self.client_weights = []

        return weight_sum, tmp_sup, tmp_unsup

def aggregate_gradients_inplace(keys, values, first, tmp, ratio):
    '''Aggregate list of tensors into model dictionary.

    Args:
        keys (list): state dictionary keys of model to which dictionaries will be summed.
        values (list): list of values to sum to model dictionary.
        first (bool): flag that indicates the first value in the dictionary.
        tmp (dict): model state dictionary that will be summed.
        ratio (float): radio to weight each client value.
    '''

    for param_key, client_dict in zip (keys, values):
        if first:
            tmp[param_key] = to_device(client_dict) * ratio
        else:
            tmp[param_key] += to_device(client_dict) * ratio

    return False, tmp
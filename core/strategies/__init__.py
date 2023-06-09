# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from .base import BaseStrategy
from .fedavg import FedAvg
from .dga import DGA
from .fedlabels import FedLabels

def select_strategy(strategy):
    if strategy.lower() == 'dga':
        return DGA
    elif strategy.lower() == 'fedavg':
        return FedAvg
    elif strategy.lower() == 'fedlabels':
        return FedLabels
    else:
        raise ValueError(f'cannot use strategy f{strategy}')
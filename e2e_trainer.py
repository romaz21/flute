# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

'''
This is the main script to run on each NCCL/GLOO thread. It will spawn either a
Server or Worker object -- the former is responsible for orchestrating and
aggregating models, where as the latter processes clients' data to generate
a new model. The Server lives on the very first thread, whereas remaining
threads contain each a diferent Worker.
'''

import argparse
import os
import shutil
import yaml
import logging
from psutil import virtual_memory

import torch
import torch.distributed as dist
from azureml.core import Run

from core import federated
from core.config import FLUTEConfig
from core.server import select_server
from core.client import Client
from experiments import make_model
from utils import (
    make_optimizer,
    init_logging,
    print_rank,
    find_pretrained_model
)
from utils.dataloaders_utils import (
    make_train_dataloader,
    get_dataset,
)
from core.evaluation import make_eval_clients

def log_run_properties(config: FLUTEConfig):
    """Log parameters on AzureML.
    
    Args:
        config (dict): config containing parameters to log.
    """

    properties = {}

    # Build properties dictionary
    mem = virtual_memory()
    properties["System memory (GB)"] = float(mem.total) / (1024**3)

    props = [
        ("server_config.num_clients_per_iteration", 0),
        ("server_config.max_iteration", 0),
        ("dp_config.eps", 0),
        ("dp_config.max_weight", 0),
        ("dp_config.min_weight", 0),
        ("server_config.optimizer_config.type", "sgd"),
        ("server_config.optimizer_config.lr", 1.0),
        ("server_config.optimizer_config.amsgrad", False),
        ("server_config.annealing_config.type", "step_lr"),
        ("server_config.annealing_config.step_interval", "epoch"),
        ("server_config.annealing_config.gamma", 1.0),
        ("server_config.annealing_config.step_size", 100),
    ]

    for (key, default) in props:
        properties[key] = config.lookup(key, default)

    # Log the properties dictionary into AzureML
    run = Run.get_context()
    for k in properties:
        run.log(k, properties[k])


def run_worker(model_path, config, task, data_path, local_rank, backend):
    """Spawn worker object that lives throughout NCCL/GLOO thread.
    
    Args:
        model_path (str): path to the pretrained model.
        config (dict): dictionary containing parameters.
        task (str): what task to solve, must be a folder of :code:`experiments`.
        data_path (str): path to data.
        local_rank (int): the rank of the NCCL/GLOO thread.
    """
    model_config = config["model_config"]
    server_config = config["server_config"]

    # Backend initialization
    WORLD_RANK = federated.rank()
    LOCAL_RANK = federated.local_rank()
    print_rank(f"Backend: {backend}")
    dist.init_process_group(backend=backend, init_method=None, rank=WORLD_RANK, world_size=federated.size())

    # Assign NCCL thread to a specific GPU
    if torch.cuda.is_available():
        print_rank(f"Assigning worker to GPU {LOCAL_RANK}")
        device = torch.device("cuda:{}".format(LOCAL_RANK))
        torch.cuda.set_device(device)

    # Make the Model to distribute to workers
    model = make_model(model_config)

    # Get evaluation datasets
    val_dataset = get_dataset(data_path, config, task, mode="val", test_only=True)
    test_dataset = get_dataset(data_path, config, task, mode="test", test_only=True)
    
    # Create list of clients for test/val -- Server need the indexes and Worker the clients list
    val_clients = list(make_eval_clients(val_dataset, config))
    test_clients = list(make_eval_clients(test_dataset, config))

    # pre-cache the training data and capture the number of clients for sampling
    num_clients = Client.get_train_dataset(data_path, config, task)
    config["server_config"]["data_config"]["num_clients"] = num_clients

    # Instantiate the Server object on the first thread
    if WORLD_RANK == 0:
        try:
            print_rank('Server data preparation')

            if 'train' in config['server_config']['data_config']:
                server_train_dataloader = make_train_dataloader(config['server_config']['data_config']['train'], data_path, task=task, clientx=None)
            else:
                server_train_dataloader = None

            idx_val_clients = list(range(len(val_clients))) # Generates indexes for val clients
            idx_test_clients = list(range(len(test_clients))) # Generates indexes for test clients

            print_rank("Prepared the dataloaders")

            # Create the optimizer on the server
            optimizer = make_optimizer(server_config["optimizer_config"], model)

            # Load a model that's already trained
            best_trained_model = find_pretrained_model(model_path, model_config)
            if best_trained_model is not None:
                model_state_dict = torch.load(best_trained_model,
                    map_location=None if torch.cuda.is_available() else torch.device("cpu"))
                model.load_state_dict(model_state_dict)

            server_type = server_config["type"]
            server_setup = select_server(server_type)  # Return the server class
            server = server_setup(
                num_clients=config['server_config']['data_config']["num_clients"],
                model=model,
                optimizer=optimizer,
                ss_scheduler=None,
                data_path=data_path,
                model_path=model_path,
                server_train_dataloader=server_train_dataloader,
                config=config,
                idx_val_clients=idx_val_clients,
                idx_test_clients=idx_test_clients,
            )
            log_run_properties(config)

        except Exception as e:
            # Be sure the other workers are shut down.
            server.terminate_workers()
            raise e

        print_rank("Launching server")
        server.run()

    else:
        # Instantiate client-processing Worker on remaining threads
        print_rank("Worker on node {}: process started".format(WORLD_RANK))
        client_config = config["client_config"]
        worker = federated.Worker(
            model=model,
            data_path=data_path,
            do_profiling=client_config.get("do_profiling", False),
            val_clients=val_clients,
            test_clients=test_clients,
            val_dataset = val_dataset,
            test_dataset = test_dataset,
            config= config,
        )
        worker.run()


if __name__ == "__main__":
    # Parse command-line arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-config", default="configs/hello_world_mlm_bert_json.yaml")
    parser.add_argument("-outputPath", default="scratch")
    parser.add_argument("-dataPath", default="testing")
    parser.add_argument("-task", default=None, help="Define the task for the run")
    parser.add_argument("-backend", default=None, help="Define the communication protocol")
    parser.add_argument("-num_skip_decoding", default=-1, type=int, help="Skip decoding in unsupervised learning mode")
    parser.add_argument("--local_rank", default=-1, type=int)

    args = parser.parse_args()
    data_path = args.dataPath
    task = args.task
    local_rank = args.local_rank
    assert args.backend in ['nccl','gloo'], f"Backend {args.backend} not recognized, please select nccl or gloo"
    backend = args.backend

    # The mount point can also be retrieved from input_datasets of the run context
    if data_path is None:
        data_path = Run.get_context().input_datasets["input"]
    print("The data can be found here: ", data_path)

    # Update the model path for the sake of AzureML
    id = Run.get_context().id
    experiment_name = "-".join(id.split("-")[-4:-2])
    experiment_root = os.path.join(args.outputPath, experiment_name)
    os.makedirs(experiment_root, exist_ok=True)
    model_path = os.path.join(experiment_root, "models")
    log_path = os.path.join(experiment_root, "log")

    os.makedirs(model_path, exist_ok=True)
    os.makedirs(log_path, exist_ok=True)

    # Make a copy of the config file into the output folder, for future reference
    cfg_out = os.path.join(experiment_root, "FLUTE_config.yaml")
    if local_rank <= 0:
        shutil.copyfile(args.config, cfg_out)
    
    # Initialize logging
    init_logging(log_path, loglevel=logging.INFO)

    with open(args.config) as f:

        cfg_dict = yaml.safe_load(f)
        config = FLUTEConfig.from_dict(cfg_dict)
        config["data_path"] = data_path
        config["output_path"] = args.outputPath
        config["model_path"]= model_path
        config["experiment_name"] = experiment_name
        config["client_config"]["task"] = task
        config["server_config"]["task"] = task
        config.validate()

        # Instantiate either Server or Worker on the thread
        run_worker(model_path, config, task, data_path, local_rank, backend)

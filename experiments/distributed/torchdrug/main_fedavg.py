import argparse
import os
import socket
import sys

import psutil
import setproctitle
import torch.nn
import wandb

sys.path.insert(0, os.path.abspath(os.path.join(os.getcwd(), "./../../../")))
sys.path.insert(0, os.path.abspath(os.path.join(os.getcwd(), "")))
# from data_preprocessing.molecule.data_loader import *
import logging
from data_preprocessing.torchdrug.data_loader import ClinToxDataLoader
from training.torchdrug.torchdrug_trainer import TorchDrugTrainer
from FedML.fedml_api.distributed.fedavg.FedAvgAPI import FedML_init

from experiments.distributed.initializer import add_federated_args, get_fl_algorithm_initializer, set_seed
from torchdrug import models, tasks;

def add_args(parser):
    """
    parser : argparse.ArgumentParser
    return a parser added with args required by fit
    """
    # Training settings
    parser.add_argument('--model', type=str, default='GIN', metavar='N',
                        help='neural network used in training')


    parser.add_argument('--dataset', type=str, default='ClinTox', metavar='N',
                        help='dataset used for training')

    parser.add_argument('--data_dir', type=str, default='~/FedGraphNN/data/molecule-datasets/',
                        help='data directory')

    parser.add_argument('--normalize_features', type=bool, default=False, help='Whether or not to symmetrically normalize feat matrices')

    parser.add_argument('--normalize_adjacency', type=bool, default=False, help='Whether or not to symmetrically normalize adj matrices')

    parser.add_argument('--sparse_adjacency', type=bool, default=False, help='Whether or not the adj matrix is to be processed as a sparse matrix')


    parser.add_argument('--batch_size', type=int, default=1, metavar='N',
                        help='input batch size for training (default: 64)')

    # model related
    parser.add_argument('--hidden_size', type=int, default=256, help='Size of GraphSAGE hidden layer')

    parser.add_argument('--node_embedding_dim', type=int, default=256,
                        help='Dimensionality of the vector space the atoms will be embedded in')

    parser.add_argument('--alpha', type=float, default=0.5, help='Alpha value for LeakyRelu used in GAT')

    parser.add_argument('--num_heads', type=int, default=2, help='Number of attention heads used in GAT')

    parser.add_argument('--dropout', type=float, default=0.3, help='Dropout used between GraphSAGE layers')

    parser.add_argument('--readout_hidden_dim', type=int, default=64, help='Size of the readout hidden layer')

    parser.add_argument('--graph_embedding_dim', type=int, default=64,
                        help='Dimensionality of the vector space the molecule will be embedded in')

    parser.add_argument('--wd', help='weight decay parameter;', type=float, default=0.001)

    parser.add_argument('--gpu_server_num', type=int, default=1,
                        help='gpu_server_num')

    parser.add_argument('--gpu_num_per_server', type=int, default=4,
                        help='gpu_num_per_server')

    parser.add_argument('--gradient_interval', type=int, default=1)
    
    parser.add_argument('--scheduler', type=str, default='None')
    parser = add_federated_args(parser)
    args = parser.parse_args()
    return args


def load_data(args, dataset_name):
    if (args.dataset != 'SIDER') and (args.dataset != 'ClinTox') and (args.dataset != 'BBPB') and \
            (args.dataset != 'BACE') and (args.dataset != 'PCBA') and (args.dataset != 'Tox21') and \
                (args.dataset != 'MUV') and (args.dataset != 'HIV') :
        raise Exception("no such dataset!")

    compact = args.model == "graphsage"

    logging.info("load_data. dataset_name = %s" % dataset_name)
    if dataset_name == 'ClinTox':
        data_loader = ClinToxDataLoader(args.data_dir)
    else:
        Exception("no such dataset!")   
        
    unif = True if args.partition_method == "homo" else False
    if args.model == "gcn":
        args.normalize_features = True
        args.normalize_adjacency = True

    if args.dataset == "pcba":
        args.metric = "prc-auc"
    else:
        args.metric = "roc-auc"

    (
        train_data_num,
        val_data_num,
        test_data_num,
        train_data_global,
        val_data_global,
        test_data_global,
        data_local_num_dict,
        train_data_local_dict,
        val_data_local_dict,
        test_data_local_dict,
    ) = data_loader.load_partition_data(
        args,
        args.client_num_in_total,
        uniform=unif,
        compact=compact,
    )

    dataset = [
        train_data_num,
        val_data_num,
        test_data_num,
        train_data_global,
        val_data_global,
        test_data_global,
        data_local_num_dict,
        train_data_local_dict,
        val_data_local_dict,
        test_data_local_dict,
    ]

    return dataset


def create_model(args, model_name, train_data_loader):
    if model_name == "GIN":
        model = models.GIN(input_dim=args.node_embedding_dim, hidden_dims=[256, 256, 256, 256],
                            short_cut=True, batch_norm=True, concat_hidden=True).cuda()
        task = tasks.PropertyPrediction(model, task=train_data_loader.dataset.dataset.tasks, criterion="bce", metric=("auprc", "auroc"))
        if hasattr(task, "preprocess"):
            result = task.preprocess(train_data_loader.dataset.dataset, None, None)
        if model.device.type == "cuda":
            task = task.cuda(model.device)
        model = task
        trainer = TorchDrugTrainer(model, args)
    else:
        raise Exception("such model does not exist !")
    logging.info("done")
    return model, trainer

def init_training_device(process_ID, fl_worker_num, gpu_num_per_machine):
    # device = torch.device("cuda:" + str(gpu_num_per_machine))
    # # initialize the mapping from process ID to GPU ID: <process ID, GPU ID>
    if process_ID == 0:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        return device
    process_gpu_dict = dict()
    for client_index in range(fl_worker_num):
        gpu_index = client_index % gpu_num_per_machine
        process_gpu_dict[client_index] = gpu_index

    logging.info(process_gpu_dict)
    device = torch.device(
        "cuda:" + str(process_gpu_dict[process_ID - 1])
        if torch.cuda.is_available()
        else "cpu"
    )
    logging.info(device)
    return device

def post_complete_message_to_sweep_process(args):
    logging.info("post_complete_message_to_sweep_process")
    pipe_path = "./torchdrug_cls"
    if not os.path.exists(pipe_path):
        os.mkfifo(pipe_path)
    pipe_fd = os.open(pipe_path, os.O_WRONLY)

    with os.fdopen(pipe_fd, "w") as pipe:
        pipe.write("training is finished! \n%s" % (str(args)))



if __name__ == "__main__":
    # initialize distributed computing (MPI)
    comm, process_id, worker_number = FedML_init()

    # parse python script input parameters
    parser = argparse.ArgumentParser()
    args = add_args(parser)

    # customize the process name
    str_process_name = "FedGraphNN:" + str(process_id)
    setproctitle.setproctitle(str_process_name)

    # customize the log format
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d - {%(module)s.py (%(lineno)d)} - %(funcName)s(): %(message)s",
        datefmt="%Y-%m-%d,%H:%M:%S",
    )
    logging.info(args)

    hostname = socket.gethostname()
    logging.info(
        "#############process ID = "
        + str(process_id)
        + ", host name = "
        + hostname
        + "########"
        + ", process ID = "
        + str(os.getpid())
        + ", process Name = "
        + str(psutil.Process(os.getpid()))
    )

    logging.info("process_id = %d, size = %d" % (process_id, worker_number))

    # initialize the wandb machine learning experimental tracking platform (https://www.wandb.com/).
    if process_id == 0:
        wandb.init(
            # project="federated_nas",
            project="fedmolecule",
            name="FedGraphNN(d)" + str(args.model) + "r" + str(args.dataset) + "-lr" + str(args.lr),
            config=args
        )

    set_seed(0)

    # GPU arrangement: Please customize this function according your own topology.
    # The GPU server list is configured at "mpi_host_file".
    # If we have 4 machines and each has two GPUs, and your FL network has 8 workers and a central worker.
    # The 4 machines will be assigned as follows:
    # machine 1: worker0, worker4, worker8;
    # machine 2: worker1, worker5;
    # machine 3: worker2, worker6;
    # machine 4: worker3, worker7;
    # Therefore, we can see that workers are assigned according to the order of machine list.
    logging.info("process_id = %d, size = %d" % (process_id, worker_number))
    device = init_training_device(
        process_id, worker_number - 1, args.gpu_num_per_server
    )

    # load data
    dataset = load_data(args, args.dataset)
    [
        train_data_num,
        val_data_num,
        test_data_num,
        train_data_global,
        val_data_global,
        test_data_global,
        data_local_num_dict,
        train_data_local_dict,
        val_data_local_dict,
        test_data_local_dict,
    ] = dataset

    # create model.
    # Note if the model is DNN (e.g., ResNet), the training will be very slow.
    # In this case, please use our FedML distributed version (./fedml_experiments/distributed_fedavg)
    args.node_embedding_dim = train_data_global.dataset.dataset.node_feature_dim
    model, trainer = create_model(args, args.model, train_data_global)
    
    # start "federated averaging (FedAvg)"
    fl_alg = get_fl_algorithm_initializer(args.fl_algorithm)
    fl_alg(process_id, worker_number, device, comm,
                             model, train_data_num, train_data_global, test_data_global,
                             data_local_num_dict, train_data_local_dict, test_data_local_dict, args,
                             trainer)

    if process_id == 0:
        post_complete_message_to_sweep_process(args)
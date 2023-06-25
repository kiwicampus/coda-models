import _init_path
import os
import torch
from tensorboardX import SummaryWriter
import time
import glob
import re
import datetime
import argparse
import numpy as np
from pathlib import Path
import torch.distributed as dist
from pcdet.datasets import build_dataloader
from pcdet.models import build_network
from pcdet.utils import common_utils
from pcdet.config import cfg, cfg_from_list, cfg_from_yaml_file, log_config_to_file
from eval_utils import eval_utils
from pcdet.models.model_utils.dsnorm import DSNorm

import wandb

def parse_config():
    parser = argparse.ArgumentParser(description='arg parser')
    parser.add_argument('--cfg_file', type=str, default=None, help='specify the config for training')

    parser.add_argument('--batch_size', type=int, default=None, required=False, help='batch size for training')
    parser.add_argument('--epochs', type=int, default=80, required=False, help='Number of epochs to train for')
    parser.add_argument('--workers', type=int, default=1, help='number of workers for dataloader')
    parser.add_argument('--extra_tag', type=str, default='default', help='extra tag for this experiment')
    parser.add_argument('--ckpt', type=str, default=None, help='checkpoint to start from')
    parser.add_argument('--launcher', choices=['none', 'pytorch', 'slurm'], default='none')
    parser.add_argument('--tcp_port', type=int, default=18888, help='tcp port for distrbuted training')
    parser.add_argument('--local_rank', type=int, default=0, help='local rank for distributed training')
    parser.add_argument('--set', dest='set_cfgs', default=None, nargs=argparse.REMAINDER,
                        help='set extra config keys if needed')

    parser.add_argument('--max_waiting_mins', type=int, default=0, help='max waiting minutes')
    parser.add_argument('--start_epoch', type=int, default=0, help='')
    parser.add_argument('--eval_tag', type=str, default='default', help='eval tag for this experiment')
    parser.add_argument('--eval_all', action='store_true', default=False, help='whether to evaluate all checkpoints')
    parser.add_argument('--ckpt_dir', type=str, default=None, help='specify a ckpt directory to be evaluated if needed')
    parser.add_argument('--save_to_file', action='store_true', default=False, help='')

    args = parser.parse_args()

    cfg_from_yaml_file(args.cfg_file, cfg)
    cfg.TAG = Path(args.cfg_file).stem
    cfg.EXP_GROUP_PATH = '/'.join(args.cfg_file.split('/')[1:-1])  # remove 'cfgs' and 'xxxx.yaml'

    np.random.seed(1024)

    if args.set_cfgs is not None:
        cfg_from_list(args.set_cfgs, cfg)

    return args, cfg


def eval_single_ckpt(model, test_loader, args, eval_output_dir, logger, epoch_id, dist_test=False, ft_cfg=None):
    # load checkpoint
    model.load_params_from_file(filename=args.ckpt, logger=logger, to_cpu=dist_test)
    model.cuda()

    # start evaluation
    eval_utils.eval_one_epoch(
        cfg, model, test_loader, epoch_id, logger, dist_test=dist_test,
        result_dir=eval_output_dir, save_to_file=args.save_to_file, args=args, ft_cfg=ft_cfg
    )


def get_no_evaluated_ckpt(ckpt_dir, ckpt_record_file, args):
    ckpt_list = glob.glob(os.path.join(ckpt_dir, '*checkpoint_epoch_*.pth'))
    # ckpt_list.sort(key=os.path.getmtime)
    # import pdb; pdb.set_trace()
    #Sort by epoch num
    ckpt_list = sorted(ckpt_list, key=lambda x:int(re.findall("(\d+)",x)[-1]), reverse=False)
    # print("ckpt list ", ckpt_list)
    evaluated_ckpt_list = [float(x.strip()) for x in open(ckpt_record_file, 'r').readlines()]

    for cur_ckpt in ckpt_list:
        num_list = re.findall('checkpoint_epoch_(.*).pth', cur_ckpt)
        if num_list.__len__() == 0:
            continue

        epoch_id = num_list[-1]
        if 'optim' in epoch_id:
            continue
        if float(epoch_id) not in evaluated_ckpt_list and int(float(epoch_id)) >= args.start_epoch:
            return epoch_id, cur_ckpt
    return -1, None


def repeat_eval_ckpt(model, test_loader, args, eval_output_dir, logger, ckpt_dir, dist_test=False, ft_cfg=None):
    # evaluated ckpt record
    ckpt_record_file = eval_output_dir / ('eval_list_%s.txt' % cfg.DATA_CONFIG.DATA_SPLIT['test'])
    with open(ckpt_record_file, 'a'):
        pass

    # tensorboard log
    if cfg.LOCAL_RANK == 0:
        tb_log = SummaryWriter(log_dir=str(eval_output_dir / ('tensorboard_%s' % cfg.DATA_CONFIG.DATA_SPLIT['test'])))
    total_time = 0
    first_eval = True

    while True:
        # check whether there is checkpoint which is not evaluated
        cur_epoch_id, cur_ckpt = get_no_evaluated_ckpt(ckpt_dir, ckpt_record_file, args)
        if cur_epoch_id == -1 or int(float(cur_epoch_id)) < args.start_epoch:
            if cfg.LOCAL_RANK == 0:
                tb_log.flush()

            wait_second = 30
            if cfg.LOCAL_RANK == 0:
                print('Wait %s seconds for next check (progress: %.1f / %d minutes): %s \r'
                      % (wait_second, total_time * 1.0 / 60, args.max_waiting_mins, ckpt_dir), end='', flush=True)
            time.sleep(wait_second)
            total_time += 30
            if total_time > args.max_waiting_mins * 60: #and (first_eval is False):
                break
            continue

        total_time = 0
        first_eval = False
        model.load_params_from_file(filename=cur_ckpt, logger=logger, to_cpu=dist_test, load_head=True)
        model.cuda()

        # start evaluation
        cur_result_dir = eval_output_dir / ('epoch_%s' % cur_epoch_id) / cfg.DATA_CONFIG.DATA_SPLIT['test']
        tb_dict = eval_utils.eval_one_epoch(
            cfg, model, test_loader, cur_epoch_id, logger, dist_test=dist_test,
            result_dir=cur_result_dir, save_to_file=args.save_to_file, args=args, ft_cfg=ft_cfg
        )

        if cfg.LOCAL_RANK == 0:
            print("cur_epoch_id ", cur_epoch_id)
            for key, val in tb_dict.items():
                tb_log.add_scalar(key, val, cur_epoch_id)

        # record this epoch which has been evaluated
        with open(ckpt_record_file, 'a') as f:
            print('%s' % cur_epoch_id, file=f)
        logger.info('Epoch %s has been evaluated' % cur_epoch_id)


def main():
    # Add automatic evaluation of multiple target datasets
    data_config_tar_list = ['DATA_CONFIG_TAR' , 'DATA1_CONFIG_TAR', 'DATA2_CONFIG_TAR', 'DATA3_CONFIG_TAR']
    launched_once = False

    for data_config_tar in data_config_tar_list:
         ### BEGIN HERE: Read args once cycle
        args, cfg = parse_config()
        if args.launcher == 'none':
            dist_test = False
            total_gpus = 1
        else:
            if not launched_once:
                total_gpus, cfg.LOCAL_RANK = getattr(common_utils, 'init_dist_%s' % args.launcher)(
                    args.tcp_port, args.local_rank, backend='nccl'
                )
                dist_test = True
                launched_once=True

        if args.batch_size is None:
            args.batch_size = cfg.OPTIMIZATION.BATCH_SIZE_PER_GPU
        else:
            print("batch size ", args.batch_size)
            assert args.batch_size % total_gpus == 0, 'Batch size should match the number of gpus'
            args.batch_size = args.batch_size // total_gpus
        ### END HERE

        if cfg.get(data_config_tar, None) is None:
            continue

    for data_config_tar in data_config_tar_list:
        if cfg.get(data_config_tar, None) is None:
            print("Missing data config %s, skipping..." % data_config_tar)
            continue
        LR = str(cfg[data_config_tar].get('LR', '0.010000'))
        OPT = cfg[data_config_tar].get('OPT', 'adam_onecycle')
        output_dir = cfg.ROOT_DIR / 'output' / cfg.EXP_GROUP_PATH / cfg.TAG / ("%sLR%sOPT%s"%(args.extra_tag, LR, OPT))
        output_dir.mkdir(parents=True, exist_ok=True)

        eval_output_dir = output_dir / 'eval'

        print("Evaluating base config ", cfg[data_config_tar].get('_BASE_CONFIG_', None))

        DATA_CONFIG_TAR_RES = cfg[data_config_tar].get('RES', 'ORIGINAL')

        if not args.eval_all:
            num_list = re.findall(r'\d+', args.ckpt) if args.ckpt is not None else []
            epoch_id = num_list[-1] if num_list.__len__() > 0 else 'no_number'
            eval_output_dir = eval_output_dir / ('epoch_%s' % epoch_id) / cfg.DATA_CONFIG.DATA_SPLIT['test']
        else:
            eval_output_dir = eval_output_dir / ('eval_all_default_%s' % str(DATA_CONFIG_TAR_RES))

        if args.eval_tag is not None:
            eval_output_dir = eval_output_dir / args.eval_tag

        eval_output_dir.mkdir(parents=True, exist_ok=True)
        log_file = eval_output_dir / ('log_eval_%s.txt' % datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
        logger = common_utils.create_logger(log_file, rank=cfg.LOCAL_RANK)

        # log to file
        logger.info('**********************Start logging**********************')
        gpu_list = os.environ['CUDA_VISIBLE_DEVICES'] if 'CUDA_VISIBLE_DEVICES' in os.environ.keys() else 'ALL'
        logger.info('CUDA_VISIBLE_DEVICES=%s' % gpu_list)

        if dist_test:
            logger.info('total_batch_size: %d' % (total_gpus * args.batch_size))
        for key, val in vars(args).items():
            logger.info('{:16} {}'.format(key, val))
        log_config_to_file(cfg, logger=logger)

        ckpt_dir = args.ckpt_dir if args.ckpt_dir is not None else output_dir / 'ckpt'

        if cfg.get(data_config_tar, None):
            test_set, test_loader, sampler = build_dataloader(
                dataset_cfg=cfg[data_config_tar],
                class_names=cfg[data_config_tar].CLASS_NAMES,
                batch_size=args.batch_size,
                dist=dist_test, workers=args.workers, logger=logger, training=False
            )
        else:
            test_set, test_loader, sampler = build_dataloader(
                dataset_cfg=cfg.DATA_CONFIG,
                class_names=cfg.CLASS_NAMES,
                batch_size=args.batch_size,
                dist=dist_test, workers=args.workers, logger=logger, training=False
            )

        model = build_network(model_cfg=cfg.MODEL, num_class=len(cfg.CLASS_NAMES), dataset=test_set)

        if cfg.get('SELF_TRAIN', None) and cfg.SELF_TRAIN.get('DSNORM', None):
            model = DSNorm.convert_dsnorm(model)

        state_name = 'model_state'

        with torch.no_grad():
            if args.eval_all:
                ft_cfg=cfg.get('FINETUNE', None)
                if ft_cfg is not None:
                    # start a new wandb run to track this script
                    wandb_name = "eval%s_lr%0.6f_opt%s_rank%i_eval_all" % (DATA_CONFIG_TAR_RES, cfg.OPTIMIZATION.LR , cfg.OPTIMIZATION.OPTIMIZER, cfg.LOCAL_RANK)
                    wandb.init(
                        # set the wandb project where this run will be logged
                        project=ft_cfg.WANDB_NAME,
                        
                        # track hyperparameters and run metadata
                        config={
                            "learning_rate": cfg.OPTIMIZATION.LR,
                            "optimizer": cfg.OPTIMIZATION.OPTIMIZER,
                            "architecture": "PVRCNN",
                            "dataset": "CODa",
                            "epochs": 50,
                            "name": wandb_name
                        },
                        name=wandb_name
                    )            
                repeat_eval_ckpt(model, test_loader, args, eval_output_dir, logger,
                                ckpt_dir, dist_test=dist_test, ft_cfg=ft_cfg)
                wandb.finish()
            else:
                eval_single_ckpt(model, test_loader, args, eval_output_dir, logger,
                                epoch_id, dist_test=dist_test)


if __name__ == '__main__':
    main()

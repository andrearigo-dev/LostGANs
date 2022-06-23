import argparse
import os
import pickle
import time
import datetime
from tensorboardX import SummaryWriter
from torchvision.utils import make_grid

from utils.util import *
from data.cocostuff_loader import *
from data.vg import *
from model.resnet_generator_v2 import *
from model.rcnn_discriminator import *
from model.sync_batchnorm import DataParallelWithCallback
from utils.logger import setup_logger
from data.datasets import get_dataset
from torchvision.utils import draw_bounding_boxes
from torchvision.transforms import Resize
import utils.depth as udpt

import wandb


# def get_dataset(dataset, img_size, num_obj=10):
#     if dataset == "coco":
#         data = CocoSceneGraphDataset(image_dir='./datasets/coco/images/train2017/',
#                                         instances_json='./datasets/coco/annotations/instances_train2017.json',
#                                         stuff_json='./datasets/coco/annotations/stuff_train2017.json',
#                                         stuff_only=True, image_size=(img_size, img_size), left_right_flip=True)
#     elif dataset == 'vg':
#         with open('./datasets/vg/vocab.json', 'r') as fj:
#             vocab = json.load(fj)

#         data = VgSceneGraphDataset(vocab=vocab, h5_path='./datasets/vg/train.h5',
#                                     image_dir='./datasets/vg/images/',
#                                     image_size=(img_size, img_size), max_objects=num_obj, left_right_flip=True)
#     return data


def main(args):

    wandb.tensorboard.patch(root_logdir=args.out_path)
    wandb.init(project='lostgan-depth', sync_tensorboard=True)

    # parameters
    img_size = 128
    z_dim = 128
    lamb_obj = 1.0
    lamb_img = 0.1
    num_classes = 184 if args.dataset == 'coco' else 179
    num_obj = 8 if args.dataset == 'coco' else 31

    disp_depth = 6
    disp_depth = disp_depth if disp_depth < args.batch_size else args.batch_size

    # data loader
    train_data = get_dataset(args.dataset, img_size, mode='train', return_depth=True, return_filenames=True)

    dataloader = torch.utils.data.DataLoader(
        train_data, batch_size=args.batch_size,
        drop_last=True, shuffle=True, num_workers=8)

    # Load model
    netG = ResnetGeneratorDepth128(num_classes=num_classes, output_dim=3).cuda()
    netD = CombineDiscriminator128(num_classes=num_classes).cuda()

    parallel = True
    if parallel:
        netG = DataParallelWithCallback(netG)
        netD = nn.DataParallel(netD)

    # learning rates
    g_lr, d_lr = args.g_lr, args.d_lr

    # generator parameters
    gen_parameters = []
    for key, value in dict(netG.named_parameters()).items():
        if value.requires_grad:
            if 'mapping' in key:
                gen_parameters += [{'params': [value], 'lr': g_lr * 0.1}]
            else:
                gen_parameters += [{'params': [value], 'lr': g_lr}]

    # generator optimizer
    g_optimizer = torch.optim.Adam(gen_parameters, betas=(0, 0.999))

    # discriminator parameters
    dis_parameters = []
    for key, value in dict(netD.named_parameters()).items():
        if value.requires_grad:
            dis_parameters += [{'params': [value], 'lr': d_lr}]
    
    # discriminator optimizer
    d_optimizer = torch.optim.Adam(dis_parameters, betas=(0, 0.999))

    # make dirs
    if not os.path.exists(args.out_path):
        os.mkdir(args.out_path)
    if not os.path.exists(os.path.join(args.out_path, 'model/')):
        os.mkdir(os.path.join(args.out_path, 'model/'))
    writer = SummaryWriter(os.path.join(args.out_path, 'log'))

    logger = setup_logger("lostGAN", args.out_path, 0)
    logger.info(netG)
    logger.info(netD)

    start_time = time.time()
    vgg_loss = VGGLoss()
    vgg_loss = nn.DataParallel(vgg_loss)
    l1_loss = nn.DataParallel(nn.L1Loss())

    for epoch in range(args.total_epoch):
        netG.train()
        netD.train()

        for idx, data in enumerate(dataloader):
            real_images, label, bbox, depths, filenames, flips = data
            real_images, label, bbox, depths = real_images.cuda(), label.long().cuda().unsqueeze(-1), bbox.float(), depths.cuda()

            # update D network
            netD.zero_grad()
            real_images, label = real_images.cuda(), label.long().cuda()
            d_out_real, d_out_robj = netD(real_images, bbox, label)
            d_loss_real = torch.nn.ReLU()(1.0 - d_out_real).mean()
            d_loss_robj = torch.nn.ReLU()(1.0 - d_out_robj).mean()

            # z_obj, random latent object appearance
            z = torch.randn(real_images.size(0), num_obj, z_dim).cuda()

            fake_images = netG(z, bbox, y=label.squeeze(dim=-1), depths=depths)
            d_out_fake, d_out_fobj = netD(fake_images.detach(), bbox, label)
            d_loss_fake = torch.nn.ReLU()(1.0 + d_out_fake).mean()
            d_loss_fobj = torch.nn.ReLU()(1.0 + d_out_fobj).mean()

            d_loss = lamb_obj * (d_loss_robj + d_loss_fobj) + lamb_img * (d_loss_real + d_loss_fake)
            d_loss.backward()
            d_optimizer.step()

            # print and log losses
            wandb.log({
                "epoch": epoch+1,
                "d_loss": d_loss.item(),
                "d_loss_real": d_loss_real.item(),
                "d_loss_fake": d_loss_fake.item(),
                "d_loss_robj": d_loss_robj.item(),
                "d_loss_fobj": d_loss_fobj.item()
                })
            
            print(f"Epoch: {epoch+1}, d_loss: {d_loss}")

            # update G network
            if (idx % 1) == 0:
                netG.zero_grad()
                g_out_fake, g_out_obj = netD(fake_images, bbox, label)
                g_loss_fake = - g_out_fake.mean()
                g_loss_obj = - g_out_obj.mean()
                
                pixel_loss = l1_loss(fake_images, real_images).mean()
                feat_loss = vgg_loss(fake_images, real_images).mean()

                g_loss = g_loss_obj * lamb_obj + g_loss_fake * lamb_img + pixel_loss + feat_loss
                g_loss.backward()
                g_optimizer.step()

                # print and log losses
                wandb.log({
                    "epoch": epoch+1,
                    "g_loss_fake": g_loss_fake.item(),
                    "g_loss_obj": g_loss_obj.item(),
                    "g_loss": g_loss.item(),
                    "pixel_loss": pixel_loss.item(),
                    "feat_loss": feat_loss.item()
                    })

                print(f"Epoch: {epoch+1}, d_loss: {g_loss}")

            if (idx+1) % 500 == 0:
                elapsed = time.time() - start_time
                elapsed = str(datetime.timedelta(seconds=elapsed))
                logger.info("Time Elapsed: [{}]".format(elapsed))
                logger.info("Step[{}/{}], d_out_real: {:.4f}, d_out_fake: {:.4f}, g_out_fake: {:.4f} ".format(epoch + 1,
                                                                                                        idx + 1,
                                                                                                        d_loss_real.item(),
                                                                                                        d_loss_fake.item(),
                                                                                                        g_loss_fake.item()))
                logger.info("             d_obj_real: {:.4f}, d_obj_fake: {:.4f}, g_obj_fake: {:.4f} ".format(
                                                                                                        d_loss_robj.item(),
                                                                                                        d_loss_fobj.item(),
                                                                                                        g_loss_obj.item()))
                logger.info("             pixel_loss: {:.4f}, feat_loss: {:.4f}".format(pixel_loss.item(), feat_loss.item()))

                # put images in a grid tensor
                # * 0.5 + 0.5 normalizes images to [0,1]
                real_grid = make_grid(real_images.cpu().data * 0.5 + 0.5, nrow=4)
                fake_grid = make_grid(fake_images.cpu().data * 0.5 + 0.5, nrow=4)

                depth_results = []
                resize_ = Resize(train_data.image_size)

                # visualize the first disp_depth images and their depth layouts
                for jdx in range(disp_depth):
                    coord_box = scale_boxes(
                        bbox[jdx], train_data.image_size, 'coordinates', dtype=torch.int)
                    
                    # draw boxes
                    ann_img = normalize_tensor(real_images[jdx].cpu()*0.5+0.5, (0, 255)).type(torch.uint8)
                    ann_img = draw_bounding_boxes(ann_img, coord_box)

                    # load depthmap
                    depthmap = torch.from_numpy(np.load(Path(train_data.depth_dir, filenames[jdx] + '.npy')))
                    depthmap = resize_(depthmap.unsqueeze(0))
                    # depthmap = depthmap.squeeze()

                    if flips[jdx]:
                        # flip the depthmap as the image is also flipped
                        depthmap = torch.fliplr(depthmap)

                    # get depth layout
                    depth_layout = udpt.get_depth_layout(depths[jdx], depthmap, bbox[jdx])

                    depth_results.extend([
                        # normalize everything to [0,1]
                        normalize_tensor(ann_img.type(torch.float32), (0,1)),
                        torch.cat((depth_layout, depth_layout, depth_layout), 0).cpu(), # already in [0,1]
                        (fake_images[jdx]*0.5+0.5).cpu()
                        ])
                
                depth_grid = make_grid(depth_results, nrow=3)

                writer.add_image("real images", real_grid, epoch*len(dataloader) + idx + 1)
                writer.add_image("fake images", fake_grid, epoch*len(dataloader) + idx + 1)
                writer.add_image("depth results", depth_grid, epoch*len(dataloader) + idx + 1)

                wandb.log({
                    "real_images": wandb.Image(real_grid),
                    "fake_images": wandb.Image(fake_grid),
                    "depth_results": wandb.Image(depth_grid)
                })

        # save model
        if (epoch + 1) % 5 == 0:
            torch.save(netG.state_dict(), os.path.join(args.out_path, 'model/', 'G_%d.pth' % (epoch+1)))
    
    wandb.finish()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, default='coco',
                        help='training dataset')
    parser.add_argument('--batch_size', type=int, default=128,
                        help='mini-batch size of training data. Default: 32')
    parser.add_argument('--total_epoch', type=int, default=200,
                        help='number of total training epoch')
    parser.add_argument('--d_lr', type=float, default=0.0001,
                        help='learning rate for discriminator')
    parser.add_argument('--g_lr', type=float, default=0.0001,
                        help='learning rate for generator')
    parser.add_argument('--out_path', type=str, default='./outputs/',
                        help='path to output files')
    parser.add_argument('--depth_path', type=str, default='',
                        help='path to depthmaps')
    args = parser.parse_args()

    # train params
    # args.dataset = 'vg'
    # args.out_path = 'outputs/out-vg/'
    
    args.dataset = 'coco'
    args.out_path = 'outputs/coco-depth/'
    args.depth_dir = Path('datasets', f'{args.dataset}-depth', 'train')

    args.batch_size = 32
    args.total_epoch = 200

    main(args)

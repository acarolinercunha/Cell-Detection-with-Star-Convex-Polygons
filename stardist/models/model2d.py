from __future__ import print_function, unicode_literals, absolute_import, division

import numpy as np
import warnings
import math
from tqdm import tqdm

from csbdeep.models import BaseConfig
from csbdeep.utils import _raise, backend_channels_last, axes_check_and_normalize, axes_dict
from csbdeep.utils.tf import keras_import, IS_TF_1, CARETensorBoard, CARETensorBoardImage
from skimage.segmentation import clear_border
from skimage.measure  import regionprops
from scipy.ndimage import zoom
from distutils.version import LooseVersion

keras = keras_import()
K = keras_import('backend')
Input, Conv2D, MaxPooling2D, Dropout = keras_import('layers', 'Input', 'Conv2D', 'MaxPooling2D','Dropout')
Model = keras_import('models', 'Model')

from .base import StarDistBase, StarDistDataBase
from ..sample_patches import sample_patches
from ..utils import edt_prob, _normalize_grid, mask_to_categorical
from ..geometry import star_dist, dist_to_coord, polygons_to_label
from ..nms import non_maximum_suppression
from .advanced_blocks import resnet_block, unet_block, fpn_block


class StarDistData2D(StarDistDataBase):

    def __init__(self, X, Y, batch_size, n_rays, length,
                 n_classes = None, classes = None,
                 patch_size=(256,256), b=32, grid=(1,1), shape_completion=False, augmenter=None, foreground_prob=0, **kwargs):

        super().__init__(X=X, Y=Y, n_rays=n_rays, grid=grid,
                         classes = classes, n_classes = n_classes, 
                         batch_size=batch_size, patch_size=patch_size, length=length,
                         augmenter=augmenter, foreground_prob=foreground_prob, **kwargs)

        self.shape_completion = bool(shape_completion)
        if self.shape_completion and b > 0:
            self.b = slice(b,-b),slice(b,-b)
        else:
            self.b = slice(None),slice(None)

        self.sd_mode = 'opencl' if self.use_gpu else 'cpp'


    def __getitem__(self, i):
        idx = self.batch(i)
        arrays = [sample_patches((self.Y[k],) + self.channels_as_tuple(self.X[k]),
                                 patch_size=self.patch_size, n_samples=1,
                                 valid_inds=self.get_valid_inds(k)) for k in idx]

        if self.n_channel is None:
            X, Y = list(zip(*[(x[0][self.b],y[0]) for y,x in arrays]))
        else:
            X, Y = list(zip(*[(np.stack([_x[0] for _x in x],axis=-1)[self.b], y[0]) for y,*x in arrays]))

        X, Y = tuple(zip(*tuple(self.augmenter(_x, _y) for _x, _y in zip(X,Y))))

            
        prob = np.stack([edt_prob(lbl[self.b]) for lbl in Y])


        if self.shape_completion:
            Y_cleared = [clear_border(lbl) for lbl in Y]
            dist      = np.stack([star_dist(lbl,self.n_rays,mode=self.sd_mode)[self.b+(slice(None),)] for lbl in Y_cleared])
            dist_mask = np.stack([edt_prob(lbl[self.b]) for lbl in Y_cleared])
        else:
            dist      = np.stack([star_dist(lbl,self.n_rays,mode=self.sd_mode) for lbl in Y])
            dist_mask = prob

            
        X = np.stack(X)
        if X.ndim == 3: # input image has no channel axis
            X = np.expand_dims(X,-1)
        prob = np.expand_dims(prob,-1)
        dist_mask = np.expand_dims(dist_mask,-1)
        
        # subsample wth given grid
        dist_mask  = dist_mask[self.ss_grid]
        prob       = prob[self.ss_grid]
        dist       = dist[self.ss_grid]
        

        # append dist_mask to dist as additional channel
        dist = np.concatenate([dist,dist_mask],axis=-1)

        if self.n_classes is None:
            return [X], [prob,dist]
        else:
            prob_class = np.stack(tuple((mask_to_categorical(y, self.n_classes, self.classes[k]) for y,k in zip(Y, idx))))

            # as it prob_class will be later upscaled, usign zoom here leads to better registered maps
            # prob_class = prob_class[self.ss_grid]
            prob_class = zoom(prob_class, tuple(1/s for s in self.ss_grid_factor)+(1,), order=0)
            
            return [X], [prob,dist, prob_class]



class Config2D(BaseConfig):
    """Configuration for a :class:`StarDist2D` model.

    Parameters
    ----------
    axes : str or None
        Axes of the input images.
    n_rays : int
        Number of radial directions for the star-convex polygon.
        Recommended to use a power of 2 (default: 32).
    n_channel_in : int
        Number of channels of given input image (default: 1).
    grid : (int,int)
        Subsampling factors (must be powers of 2) for each of the axes.
        Model will predict on a subsampled grid for increased efficiency and larger field of view.
    n_classes : None or int
        Number of fg classes to use for multi_class predcition (use None to disable)
    backbone : str
        Name of the neural network architecture to be used as backbone.
    kwargs : dict
        Overwrite (or add) configuration attributes (see below).


    Attributes
    ----------
    unet_n_depth : int
        Number of U-Net resolution levels (down/up-sampling layers).
    unet_kernel_size : (int,int)
        Convolution kernel size for all (U-Net) convolution layers.
    unet_n_filter_base : int
        Number of convolution kernels (feature channels) for first U-Net layer.
        Doubled after each down-sampling layer.
    unet_pool : (int,int)
        Maxpooling size for all (U-Net) convolution layers.
    net_conv_after_unet : int
        Number of filters of the extra convolution layer after U-Net (0 to disable).
    unet_* : *
        Additional parameters for U-net backbone.
    train_shape_completion : bool
        Train model to predict complete shapes for partially visible objects at image boundary.
    train_completion_crop : int
        If 'train_shape_completion' is set to True, specify number of pixels to crop at boundary of training patches.
        Should be chosen based on (largest) object sizes.
    train_patch_size : (int,int)
        Size of patches to be cropped from provided training images.
    train_background_reg : float
        Regularizer to encourage distance predictions on background regions to be 0.
    train_foreground_only : float
        Fraction (0..1) of patches that will only be sampled from regions that contain foreground pixels.
    train_dist_loss : str
        Training loss for star-convex polygon distances ('mse' or 'mae').
    train_loss_weights : tuple of float
        Weights for losses relating to (probability, distance)
    train_epochs : int
        Number of training epochs.
    train_steps_per_epoch : int
        Number of parameter update steps per epoch.
    train_learning_rate : float
        Learning rate for training.
    train_batch_size : int
        Batch size for training.
    train_n_val_patches : int
        Number of patches to be extracted from validation images (``None`` = one patch per image).
    train_tensorboard : bool
        Enable TensorBoard for monitoring training progress.
    train_reduce_lr : dict
        Parameter :class:`dict` of ReduceLROnPlateau_ callback; set to ``None`` to disable.
    use_gpu : bool
        Indicate that the data generator should use OpenCL to do computations on the GPU.

        .. _ReduceLROnPlateau: https://keras.io/callbacks/#reducelronplateau
    """

    def __init__(self, axes='YX', n_rays=32, n_channel_in=1, grid=(1,1), n_classes = None,  backbone='unet', **kwargs):
        """See class docstring."""

        super().__init__(axes=axes, n_channel_in=n_channel_in, n_channel_out=1+n_rays)

        # directly set by parameters
        self.n_rays                    = int(n_rays)
        self.grid                      = _normalize_grid(grid,2)
        self.backbone                  = str(backbone).lower()
        self.n_classes                 = None if n_classes is None else int(n_classes)

        # default config (can be overwritten by kwargs below)
        if self.backbone in ('unet','seunet','fpn','sefpn'):
            self.unet_n_depth          = 3
            self.unet_kernel_size      = 3,3
            self.unet_n_filter_base    = 32
            self.unet_n_conv_per_depth = 2
            self.unet_pool             = 2,2
            self.unet_activation       = 'relu'
            self.unet_last_activation  = 'relu'
            self.unet_batch_norm       = False
            self.unet_dropout          = 0.0
            self.unet_prefix           = ''
            self.net_conv_after_unet   = 128
        else:
            pass
            # # TODO: resnet backbone for 2D model?
            # raise ValueError("backbone '%s' not supported." % self.backbone)

        # net_mask_shape not needed but kept for legacy reasons
        if backend_channels_last():
            self.net_input_shape       = None,None,self.n_channel_in
            self.net_mask_shape        = None,None,1
        else:
            self.net_input_shape       = self.n_channel_in,None,None
            self.net_mask_shape        = 1,None,None

        self.train_shape_completion    = False
        self.train_completion_crop     = 32
        self.train_patch_size          = 256,256
        self.train_background_reg      = 1e-4
        self.train_foreground_only     = 0.9

        self.train_dist_loss           = 'mae'
        self.train_loss_weights        = (1,0.2) if self.n_classes is None else (1,0.2,1)
        self.train_class_weights       = (1,1) if self.n_classes is None else (1,)*(self.n_classes+1)
        self.train_epochs              = 400
        self.train_steps_per_epoch     = 100
        self.train_learning_rate       = 0.0003
        self.train_batch_size          = 4
        self.train_n_val_patches       = None
        self.train_tensorboard         = True
        # the parameter 'min_delta' was called 'epsilon' for keras<=2.1.5
        min_delta_key = 'epsilon' if LooseVersion(keras.__version__)<=LooseVersion('2.1.5') else 'min_delta'
        self.train_reduce_lr           = {'factor': 0.5, 'patience': 40, min_delta_key: 0}

        self.use_gpu                   = False

        # remove derived attributes that shouldn't be overwritten
        for k in ('n_dim', 'n_channel_out'):
            try: del kwargs[k]
            except KeyError: pass
        
        self.update_parameters(False, **kwargs)

        # FIXME: put into is_valid()
        if not len(self.train_loss_weights) == (2 if self.n_classes is None else 3):
            raise ValueError(f"Wrong length of train_loss_weights={self.train_loss_weights} for n_classes={self.n_classes} (e.g. has to be 3 if n_classes is set)")

        if not len(self.train_class_weights) == (2 if self.n_classes is None else self.n_classes+1):
            raise ValueError(f"Wrong length of train_class_weights={self.train_class_weights} for n_classes={self.n_classes} (has to be {self.n_classes+1})")

        

class StarDist2D(StarDistBase):
    """StarDist2D model.

    Parameters
    ----------
    config : :class:`Config` or None
        Will be saved to disk as JSON (``config.json``).
        If set to ``None``, will be loaded from disk (must exist).
    name : str or None
        Model name. Uses a timestamp if set to ``None`` (default).
    basedir : str
        Directory that contains (or will contain) a folder with the given model name.

    Raises
    ------
    FileNotFoundError
        If ``config=None`` and config cannot be loaded from disk.
    ValueError
        Illegal arguments, including invalid configuration.

    Attributes
    ----------
    config : :class:`Config`
        Configuration, as provided during instantiation.
    keras_model : `Keras model <https://keras.io/getting-started/functional-api-guide/>`_
        Keras neural network model.
    name : str
        Model name.
    logdir : :class:`pathlib.Path`
        Path to model folder (which stores configuration, weights, etc.)
    """

    def __init__(self, config=Config2D(), name=None, basedir='.'):
        """See class docstring."""
        super().__init__(config, name=name, basedir=basedir)


    def _build(self):
        if self.config.backbone == "unet":
            return self._build_unet()
        elif self.config.backbone == "resnet":
            return self._build_resnet()
        elif self.config.backbone == "seresnet":
            return self._build_resnet(use_SE=True)
        elif self.config.backbone == "seunet":
            return self._build_unet(use_SE = True)
        elif self.config.backbone == "fpn":
            return self._build_fpn()
        elif self.config.backbone == "sefpn":
            return self._build_fpn(use_SE = True)
        else:
            raise NotImplementedError(self.config.backbone)

    def _build_unet(self, use_SE=False):
        self.config.backbone in ('unet',"seunet") or _raise(NotImplementedError())
        
        unet_kwargs = {k[len('unet_'):]:v for (k,v) in vars(self.config).items() if k.startswith('unet_')}

        unet_kwargs.setdefault("use_SE",use_SE)
        
        input_img  = Input(self.config.net_input_shape, name='input')

        # maxpool input image to grid size
        pooled = np.array([1,1])
        pooled_img = input_img
        while tuple(pooled) != tuple(self.config.grid):
            pool = 1 + (np.asarray(self.config.grid) > pooled)
            pooled *= pool
            for _ in range(self.config.unet_n_conv_per_depth):
                pooled_img = Conv2D(self.config.unet_n_filter_base, self.config.unet_kernel_size,
                                    padding='same', activation=self.config.unet_activation)(pooled_img)
            pooled_img = MaxPooling2D(pool)(pooled_img)

        unet_base        = unet_block(**unet_kwargs)(pooled_img)

        if self.config.net_conv_after_unet > 0:
            unet = Conv2D(self.config.net_conv_after_unet, self.config.unet_kernel_size,
                             name='features', padding='same',
                             activation=self.config.unet_activation)(unet_base)
        else:
            unet = unet_base
            
        output_prob  = Conv2D(1,                  (1,1), name='prob', padding='same',
                              activation='sigmoid')(unet)
        output_dist  = Conv2D(self.config.n_rays, (1,1), name='dist', padding='same',
                              activation='linear')(unet)
        
        # attach extra classification head when self.n_classes is given 
        if self._is_multiclass():
            if self.config.net_conv_after_unet > 0:                
                unet_class = Dropout(self.config.unet_dropout)(unet_base)
                unet_class = Conv2D(self.config.net_conv_after_unet, self.config.unet_kernel_size,
                             name='features_class', padding='same', activation=self.config.unet_activation)(unet_class)
            else:
                unet_class  = unet_base

            output_prob_class  = Conv2D(self.config.n_classes+1, (1,1),
                                        name='prob_class', padding='same',
                                        activation='softmax')(unet_class)
            return Model([input_img], [output_prob,output_dist, output_prob_class])
        
        else:
            return Model([input_img], [output_prob,output_dist])

    def _build_fpn(self, use_SE=False):
        self.config.backbone in ('fpn','sefpn') or _raise(NotImplementedError())
                
        unet_kwargs = {k[len('unet_'):]:v for (k,v) in vars(self.config).items() if k.startswith('unet_')}
        unet_kwargs.setdefault("use_SE",use_SE)

        input_img  = Input(self.config.net_input_shape, name='input')

        # maxpool input image to grid size
        pooled = np.array([1,1])
        pooled_img = input_img
        while tuple(pooled) != tuple(self.config.grid):
            pool = 1 + (np.asarray(self.config.grid) > pooled)
            pooled *= pool
            for _ in range(self.config.unet_n_conv_per_depth):
                pooled_img = Conv2D(self.config.unet_n_filter_base, self.config.unet_kernel_size,
                                    padding='same', activation=self.config.unet_activation)(pooled_img)
            pooled_img = MaxPooling2D(pool)(pooled_img)

        unet_base        = fpn_block(**unet_kwargs)(pooled_img)

        if self.config.net_conv_after_unet > 0:
            unet = Conv2D(self.config.net_conv_after_unet, self.config.unet_kernel_size,
                             name='features', padding='same',
                             activation=self.config.unet_activation)(unet_base)
        else:
            unet = unet_base
            
        output_prob  = Conv2D(1,                  (1,1), name='prob', padding='same',
                              activation='sigmoid')(unet)
        output_dist  = Conv2D(self.config.n_rays, (1,1), name='dist', padding='same',
                              activation='linear')(unet)
        
        # attach extra classification head when self.n_classes is given 
        if self._is_multiclass():
            if self.config.net_conv_after_unet > 0:                
                unet_class = Dropout(self.config.unet_dropout)(unet_base)
                unet_class = Conv2D(max(1,self.config.net_conv_after_unet//4),
                                    self.config.unet_kernel_size,
                                    name='features_class', padding='same',
                                    activation=self.config.unet_activation)(unet_class)
            else:
                unet_class  = unet_base

            output_prob_class  = Conv2D(self.config.n_classes+1, (1,1),
                                        name='prob_class', padding='same',
                                        activation='softmax')(unet_class)
            return Model([input_img], [output_prob,output_dist, output_prob_class])
        
        else:
            return Model([input_img], [output_prob,output_dist])


    def _build_resnet(self, use_SE=False):
        self.config.backbone in ('resnet',"seresnet") or _raise(NotImplementedError())
                    
        self.config.resnet_kernel_size = (3,3)
        self.config.resnet_n_conv_per_block =2
        self.config.resnet_batch_norm = False
        self.config.resnet_kernel_init = "he_normal"
        self.config.resnet_activation = "relu"
        self.config.resnet_n_blocks = 4
        self.config.resnet_n_filter_base = 48
        self.config.net_conv_after_resnet   = 128
        
        n_filter = self.config.resnet_n_filter_base
        resnet_kwargs = dict (
            kernel_size        = self.config.resnet_kernel_size,
            n_conv_per_block   = self.config.resnet_n_conv_per_block,
            batch_norm         = self.config.resnet_batch_norm,
            kernel_initializer = self.config.resnet_kernel_init,
            activation         = self.config.resnet_activation,
        )
        resnet_kwargs.setdefault("use_SE",use_SE)

        input_img = Input(self.config.net_input_shape, name='input')

        layer = input_img
        layer = Conv2D(n_filter, (7,7), padding="same", kernel_initializer=self.config.resnet_kernel_init)(layer)
        layer = Conv2D(n_filter, self.config.resnet_kernel_size, padding="same", kernel_initializer=self.config.resnet_kernel_init)(layer)

        pooled = np.array([1,1])
        for n in range(self.config.resnet_n_blocks):
            pool = 1 + (np.asarray(self.config.grid) > pooled)
            pooled *= pool
            if any(p > 1 for p in pool):
                n_filter *= 2
                
            layer = resnet_block(n_filter, pool=tuple(pool), **resnet_kwargs)(layer)

        layer_base = layer 
        if self.config.net_conv_after_resnet > 0:
            layer = Conv2D(self.config.net_conv_after_resnet, self.config.resnet_kernel_size,
                           name='features', padding='same', activation=self.config.resnet_activation)(layer_base)

        output_prob = Conv2D(1,                  (1,1), name='prob', padding='same', activation='sigmoid')(layer)
        output_dist = Conv2D(self.config.n_rays, (1,1), name='dist', padding='same', activation='linear')(layer)

        # attach extra classification head when self.n_classes is given 
        if self._is_multiclass():
            if self.config.net_conv_after_resnet > 0:
                layer_class  = Conv2D(self.config.net_conv_after_resnet, self.config.resnet_kernel_size,
                           name='features_class', padding='same', activation=self.config.resnet_activation)(layer_base)
            else:
                layer_class  = layer_base

            output_prob_class  = Conv2D(self.config.n_classes+1, (1,1),
                                        name='prob_class', padding='same',
                                        activation='softmax')(layer_class)
            return Model([input_img], [output_prob,output_dist, output_prob_class])
        
        else:
            return Model([input_img], [output_prob,output_dist])

        
    def train(self, X, Y, validation_data, classes = "auto", augmenter=None, seed=None, epochs=None, steps_per_epoch=None):
        """Train the neural network with the given data.

        Parameters
        ----------
        X : tuple, list, `numpy.ndarray`, `keras.utils.Sequence`
            Input images
        Y : tuple, list, `numpy.ndarray`, `keras.utils.Sequence`
            Label masks
        classes (optional): "auto" or iterable of same length as X 
             label -> class mapping for each mask if multiclass prediction is activated (n_classes>0)
             list of dicts with label -> class id (1,...,n_classes) 
             "auto" -> all objects will be assigned to the first foreground class 
        validation_data : tuple(:class:`numpy.ndarray`, :class:`numpy.ndarray`) or triple (if multiclass)
            Tuple (triple if multiclass) of X,Y validation arrays.
        augmenter : None or callable
            Function with expected signature ``xt, yt = augmenter(x, y)``
            that takes in a single pair of input/label image (x,y) and returns
            the transformed images (xt, yt) for the purpose of data augmentation
            during training. Not applied to validation images.
            Example:
            def simple_augmenter(x,y):
                x = x + 0.05*np.random.normal(0,1,x.shape)
                return x,y
        seed : int
            Convenience to set ``np.random.seed(seed)``. (To obtain reproducible validation patches, etc.)
        epochs : int
            Optional argument to use instead of the value from ``config``.
        steps_per_epoch : int
            Optional argument to use instead of the value from ``config``.

        Returns
        -------
        ``History`` object
            See `Keras training history <https://keras.io/models/model/#fit>`_.

        """
        if seed is not None:
            # https://keras.io/getting-started/faq/#how-can-i-obtain-reproducible-results-using-keras-during-development
            np.random.seed(seed)
        if epochs is None:
            epochs = self.config.train_epochs
        if steps_per_epoch is None:
            steps_per_epoch = self.config.train_steps_per_epoch

        if self._is_multiclass() and classes is None:
            warnings.warn("Ignoring given classes as n_classes is set to None")

        classes = self._parse_classes_arg(classes, len(X))

        validation_data is not None or _raise(ValueError())
        
        ((isinstance(validation_data,(list,tuple)) and len(validation_data)== (2 if self.config.n_classes is None else 3))
            or _raise(ValueError(f'len(validation_data) = {len(validation_data)} but should be {"2" if self.config.n_classes is None else "3"}')))

        patch_size = self.config.train_patch_size
        axes = self.config.axes.replace('C','')
        b = self.config.train_completion_crop if self.config.train_shape_completion else 0
        div_by = self._axes_div_by(axes)
        [(p-2*b) % d == 0 or _raise(ValueError(
            "'train_patch_size' - 2*'train_completion_crop' must be divisible by {d} along axis '{a}'".format(a=a,d=d) if self.config.train_shape_completion else
            "'train_patch_size' must be divisible by {d} along axis '{a}'".format(a=a,d=d)
         )) for p,d,a in zip(patch_size,div_by,axes)]

        if not self._model_prepared:
            self.prepare_for_training()

        data_kwargs = dict (
            n_rays           = self.config.n_rays,
            patch_size       = self.config.train_patch_size,
            grid             = self.config.grid,
            shape_completion = self.config.train_shape_completion,
            b                = self.config.train_completion_crop,
            use_gpu          = self.config.use_gpu,
            foreground_prob  = self.config.train_foreground_only,
            n_classes        = self.config.n_classes
        )

        # generate validation data and store in numpy arrays
        n_data_val = len(validation_data[0])
        n_take = self.config.train_n_val_patches if self.config.train_n_val_patches is not None else n_data_val
        classes_val = self._parse_classes_arg(validation_data[2], len(validation_data[0])) if self._is_multiclass() else None

        _data_val = StarDistData2D(validation_data[0],validation_data[1],
                                   classes = classes_val,
                                   batch_size=n_take, length=1, **data_kwargs)
        
        data_val = _data_val[0]

        data_train = StarDistData2D(X, Y, classes = classes,
                                    batch_size=self.config.train_batch_size, augmenter=augmenter,
                                    length=epochs*steps_per_epoch, **data_kwargs)

        if self.config.train_tensorboard:
            # show dist for three rays
            _n = min(3, self.config.n_rays)
            channel = axes_dict(self.config.axes)['C']
            output_slices = [[slice(None)]*4,[slice(None)]*4]
            output_slices[1][1+channel] = slice(0,(self.config.n_rays//_n)*_n,
                                                self.config.n_rays//_n)            
            if self._is_multiclass():
                _n = min(3, self.config.n_classes)
                output_slices += [[slice(None)]*4]
                output_slices[2][1+channel] = slice(1,1+((self.config.n_classes+1)//_n)*_n,
                                                    self.config.n_classes//_n)

            if IS_TF_1:
                for cb in self.callbacks:
                    if isinstance(cb,CARETensorBoard):
                        cb.output_slices = output_slices
                        # target image for dist includes dist_mask and thus has more channels than dist output
                        cb.output_target_shapes = [None,[None]*4, None]
                        cb.output_target_shapes[1][1+channel] = data_val[1][1].shape[1+channel]
            elif self.basedir is not None and not any(isinstance(cb,CARETensorBoardImage) for cb in self.callbacks):
                self.callbacks.append(CARETensorBoardImage(model=self.keras_model,
                                        data=data_val, log_dir=str(self.logdir/'logs'/'images'),
                                        n_images=3, prob_out=False, output_slices=output_slices))

        fit = self.keras_model.fit_generator if IS_TF_1 else self.keras_model.fit

        history = fit(iter(data_train), validation_data=data_val,
                      epochs=epochs, steps_per_epoch=steps_per_epoch,
                      callbacks=self.callbacks, verbose=1)
        self._training_finished()

        return history


    def _instances_from_prediction(self, img_shape, prob, dist, prob_class = None,
                                   prob_thresh=None, nms_thresh=None, overlap_label=None, **nms_kwargs):
        if prob_thresh is None: prob_thresh = self.thresholds.prob
        if nms_thresh  is None: nms_thresh  = self.thresholds.nms
        if overlap_label is not None: raise NotImplementedError("overlap_label not supported for 2D yet!")

        coord = dist_to_coord(dist, grid=self.config.grid)
        inds = non_maximum_suppression(coord, prob, grid=self.config.grid,
                                       prob_thresh=prob_thresh, nms_thresh=nms_thresh, **nms_kwargs)
        labels = polygons_to_label(coord, prob, inds, shape=img_shape)
        # sort 'inds' such that ids in 'labels' map to entries in polygon dictionary entries
        inds = inds[np.argsort(prob[inds[:,0],inds[:,1]])]
        # adjust for grid
        points = inds*np.array(self.config.grid)

        res_dict = dict(coord=coord[inds[:,0],inds[:,1]], points=points,
                            prob=prob[inds[:,0],inds[:,1]])

        if prob_class is not None:
            # build the list of class ids per label via majority vote
            # zoom prob_class to img_shape
            prob_class_up = zoom(prob_class,
                                 tuple(s2/s1 for s1, s2 in zip(prob_class.shape[:2], img_shape))+(1,),
                                 order=0)
            class_prob, label_ids = [],[]
            for reg in regionprops(labels):
                m = labels[reg.slice]==reg.label
                # use average class prob per object (maybe better to use center one?)
                p = np.mean(prob_class_up[reg.slice][m],axis=0)
                class_prob.append(p)
                label_ids.append(reg.label)                
            # just a sanity check whether labels where in sorted order
            assert all(x <= y for x,y in zip(label_ids, label_ids[1:]))
            class_prob = np.array(class_prob).reshape((-1,prob_class.shape[-1]))
            res_dict.update(dict(class_prob = class_prob))
            
        return labels, res_dict


    def _axes_div_by(self, query_axes):
        if self.config.backbone in ("unet","seunet","fpn","sefpn"):
            query_axes = axes_check_and_normalize(query_axes)
            assert len(self.config.unet_pool) == len(self.config.grid)
            div_by = dict(zip(
                self.config.axes.replace('C',''),
                tuple(p**self.config.unet_n_depth * g for p,g in zip(self.config.unet_pool,self.config.grid))
            ))
            return tuple(div_by.get(a,1) for a in query_axes)
        elif self.config.backbone in ("resnet","seresnet"):
            grid_dict = dict(zip(self.config.axes.replace('C',''), self.config.grid))
            return tuple(grid_dict.get(a,1) for a in query_axes)
        else:
            raise NotImplementedError()

    # def _axes_tile_overlap(self, query_axes):
    #     self.config.backbone == 'unet' or _raise(NotImplementedError())
    #     query_axes = axes_check_and_normalize(query_axes)
    #     assert len(self.config.unet_pool) == len(self.config.grid) == len(self.config.unet_kernel_size)
    #     # TODO: compute this properly when any value of grid > 1
    #     # all(g==1 for g in self.config.grid) or warnings.warn('FIXME')
    #     overlap = dict(zip(
    #         self.config.axes.replace('C',''),
    #         tuple(tile_overlap(self.config.unet_n_depth + int(np.log2(g)), k, p)
    #               for p,k,g in zip(self.config.unet_pool,self.config.unet_kernel_size,self.config.grid))
    #     ))
    #     return tuple(overlap.get(a,0) for a in query_axes)


    @property
    def _config_class(self):
        return Config2D

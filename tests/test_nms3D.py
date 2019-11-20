import numpy as np
import pytest
from stardist import non_maximum_suppression_3d, non_maximum_suppression_3d_sparse, polyhedron_to_label
from stardist import Rays_GoldenSpiral
from utils import random_image, check_similar

def create_random_data(shape = (64,65,67), noise = .1, n_rays = 32):
    dist = 10*np.ones(shape+(n_rays,))
    noise=  np.clip(noise, 0,1)
    dist *= (1+noise*np.random.uniform(-1,1, dist.shape))
    prob = np.random.uniform(0,1,shape)
    rays = Rays_GoldenSpiral(dist.shape[-1])
    return prob, dist, rays 

def create_random_suppressed(nms_thresh = .4, shape = (64,65,67), noise = .1, n_rays = 32):
    prob, dist, rays  = create_random_data(shape, noise, n_rays)
    
    points, probi, disti = non_maximum_suppression_3d(dist, prob, rays,
                                                      prob_thresh=0.9,
                                                      nms_thresh = nms_thresh,
                                                      verbose=True)
    return points, probi, disti, rays
    

@pytest.mark.parametrize('n_rays, nms_thresh, shape',
                         [(5,  0, (33,44,55)),
                          (14,.2, (43,31,34)),
                          (22,.4, (33,44,55)),
                          (32,.6, (33,44,55))])
def test_nms(n_rays, nms_thresh, shape):
    points, probi, disti, rays = create_random_suppressed(nms_thresh, shape = shape, noise = 0, n_rays = n_rays)
    return points, rays, disti

def test_label():
    n_rays = 32 
    dist = 20*np.ones((1,n_rays))
    rays = Rays_GoldenSpiral(dist.shape[-1])
    points = [[20,20,20]]
    return polyhedron_to_label(dist,points, rays, shape = (33,44,55))



def test_nms_and_label(nms_thresh=0.1, shape = (33,44,55),noise = .1, n_rays = 32):
    points, probi, disti, rays = create_random_suppressed(nms_thresh, shape = shape, noise = noise, n_rays = n_rays)
    lbl = polyhedron_to_label(disti,points, rays, shape = shape)
    return lbl

def test_speed(noises = (0,0.1,.2)):
    from time import time

    def _bench(noise):
        t = time()
        create_random_suppressed(.3, shape = (22,33,44), noise = noise, n_rays = 32)
        return time()-t

    ts = tuple(map(_bench, noises))
    for t, noise in zip(ts, noises):
        print("noise = {noise:.2f}\t t = {t:.2f} s".format(noise=noise,t=t))        
    return ts

@pytest.mark.parametrize('b', (None,2))
@pytest.mark.parametrize('grid', ((1,1,1),(1,2,4)))
def test_dense_sparse(b, grid):
    from stardist.nms import _ind_prob_thresh
    
    prob, dist, rays  = create_random_data(shape = (22,33,44), noise = .2, n_rays = 32)

    nms_thresh  = 0.3
    prob_thresh = 0.9
    
    points1, probi1, disti1 = non_maximum_suppression_3d(dist, prob, rays,
                                                         prob_thresh=prob_thresh,
                                                         grid = grid,
                                                         b = b, 
                                                         nms_thresh = nms_thresh,
                                                         verbose=True)

    inds   = _ind_prob_thresh(prob, prob_thresh, b=b)
    points = np.stack(np.where(inds), axis=1)
    points = (points * np.array(grid).reshape((1,3)))

    
    points2, probi2, disti2 = non_maximum_suppression_3d_sparse(dist[inds], prob[inds],
                                                    points, 
                                                    rays,
                                                    nms_thresh = nms_thresh,
                                                    verbose=True)
    assert np.allclose(points1, points2)
    assert np.allclose(probi1, probi2)
    assert np.allclose(disti1, disti2)
    return points1, points2


if __name__ == '__main__':
    np.random.seed(42)
    # lbl = test_nms_and_label(.2,shape = (44,55,66), noise = .2)
    # test_speed((0.2,))

    p1, p2 = test_dense_sparse(b=None, grid = (1,1,1))

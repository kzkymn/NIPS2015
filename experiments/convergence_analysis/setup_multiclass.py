import numpy as np
import matplotlib.pyplot as plt
import GPy
import mcmcGP
import ahmc
import aa_scheme
from time import time

import os
import pickle
import cPickle

from jug import TaskGenerator
import jug



from fastMultiClassLikelihood import likelihoodQuadrature, predictiveQuadrature

class Multiclass(GPy.likelihoods.Likelihood):
    def __init__(self, gp_link=None):
        if gp_link is not None:
            raise ValueError, "this likelihood assumes a robust-max inverse-link"

        super(Multiclass, self).__init__(GPy.likelihoods.link_functions.Identity(), 'Bernoulli')
        self.delta = GPy.core.Param('delta', 0.01)
        self.link_parameter(self.delta)

    def variational_expectations(self, Y, m, v, gh_points=None, Y_metadata=None):
        if gh_points is None:
            gh_x, gh_w = self._gh_points()
        else:
            gh_x, gh_w = gh_points
        ret = likelihoodQuadrature(m,v, np.array(Y.flatten(), dtype=np.int), self.delta*1., gh_w, gh_x )
        ret = list(ret)
        ret[-1] = np.array([ret[-1]]).reshape(1,1,1)
        return ret

    def update_gradients(self, g):
        self.delta.gradient = g

    def predictive_values(self, mu, var, full_cov=False, Y_metadata=None):
        assert full_cov is False, "cannot make correlated predictions"
        return predictiveQuadrature(mu, var, self.delta*1, *self._gh_points()[::-1] ), None


class Dataset:
    def __init__(self, xtrain, ytrain, xtest, ytest, ntrain, dtrain):
        self.xtrain, self.ytrain, self.xtest, self.ytest, self.ntrain, self.dtrain =  xtrain, ytrain, xtest, ytest, ntrain, dtrain

## ********** Load multiclass dataset
def load_data():
    data = cPickle.load( open('../simple_classification/data','r') )

    print "Multiclass dataset loaded\n"
    return Dataset(data['X_train'], data['Y_train'].reshape(-1,1), data['X_test'], data['Y_test'].reshape(-1,1), data['X_train'].shape[0], data['X_train'].shape[1])
## ******************************


## ********** Setup kernel priors
def kernel_priors(kernel):
    kernel.white.variance.fix(warning=False)

    kernel.rbf.variance.set_prior(GPy.priors.Gamma(3,3), warning=False)
    kernel.rbf.lengthscale.set_prior(GPy.priors.Gamma(3,2), warning=False)

    return (kernel)
## ******************************


## ********** Setup kernel
def setup_kernel(dataset, ARD):
    kernel = GPy.kern.RBF(input_dim=dataset.xtrain.shape[1], ARD=ARD) + GPy.kern.White(1, variance=1e-3)

    kernel = kernel_priors(kernel)

    return (kernel)
## ******************************


## ********** Kmeans on the input to select inducing points
def select_Z(dataset, OPTION_NZ):
    from scipy.cluster.vq import kmeans as scipy_kmeans
    
    np.random.seed(seed=149221)
    Z, _ = scipy_kmeans(dataset.xtrain, OPTION_NZ)

    return (Z)
## ******************************


## ********** Create and optimise GP classifier
def single_optimisation_GP_classifier(m_approx):
    m_approx.optimize('bfgs', messages=1, max_iters=10000, gtol=0)

def optimise_GP_classifier(dataset, Z, likelihood, kernel, OPTION_ARD):
    print "Training SVGP classifier with ", np.where(OPTION_ARD, "ARD", "NON-ARD"), "covariance..."

    np.random.seed(seed=189243)
    
    m_approx = GPy.core.SVGP(X=dataset.xtrain, Y=dataset.ytrain, Z=Z, kernel=kernel, likelihood=likelihood, num_latent_functions=3)
    m_approx.Z.fix()
    m_approx.optimize('bfgs', max_iters=100, messages=1)
    m_approx.Z.unfix()
    m_approx.optimize('bfgs', max_iters=1000, messages=1)

    print "done\n"

    return (m_approx)

def predict_approximate_GP_classifier(dataset, m_approx):
    predictions_approx = m_approx.predict(dataset.xtest)
    accuracy_approx = 100.0 * sum((predictions_approx[0] > 0.5) == dataset.ytest)[0] / dataset.ytest.size
    print "Classification accuracy =", accuracy_approx, "%\n\n" 

    return (m_approx)
## ******************************


## ********** Set mcmc structure up
def setup_mcmc_structure(dataset, Z, likelihood, kernel):
    np.random.seed(seed=64347)

    m_mcmc = mcmcGP.SGPMCMC(X=dataset.xtrain, Y=dataset.ytrain, Z=Z, kernel=kernel, likelihood=likelihood, num_latent_functions=3)
    
    m_mcmc.Z.fix()
    
    return (m_mcmc)
## ******************************


## ********** MAP on the parameters and Gaussian approximation to the posterior from VB
## WARNING - THIS ASSUMES THAT q IS THE APPROXIMATION OVER WHITENED LATENT VARIABLES
def compute_gaussian_approx_post_MAP(m_approx, m_mcmc):

    nvars = m_mcmc.optimizer_array.size
    nlatent = m_mcmc.V.size
    ntheta = nvars - nlatent
    
    nclasses = m_approx.q_u_mean.shape[1]
    nin = m_approx.q_u_mean.shape[0]

    posterior_mean = np.concatenate((m_mcmc.kern.optimizer_array, m_approx.q_u_mean.flatten(1)))
    
    posterior_cholesky_cov = 1.0e-4 * np.eye(nvars)
    for i in range(nclasses):
        tmp = (m_approx.q_u_chol.T)[i,]
        posterior_cholesky_cov[(ntheta+i*nin):(ntheta+(i+1)*nin),(ntheta+i*nin):(ntheta+(i+1)*nin)] = GPy.util.choleskies.flat_to_triang(tmp.reshape((tmp.size,1)))[0, :, :]

    posterior_cov = np.dot(posterior_cholesky_cov, posterior_cholesky_cov.T)

    return (posterior_mean, posterior_cov, posterior_cholesky_cov)



## ********** Initialise from a Gaussian approximation
def initialise_from_approx(gaussian_approx_post, OPTION_SEED=-9999):
   posterior_mean = gaussian_approx_post[0]
   posterior_cov = gaussian_approx_post[1]
   posterior_cholesky_cov = gaussian_approx_post[2]
   
   nvar_mcmc = posterior_mean.size

   if(OPTION_SEED != -9999):
       np.random.seed(seed=OPTION_SEED * 234 + 1892)
       init_sampler = np.dot(posterior_cholesky_cov, np.random.randn(nvar_mcmc)) + posterior_mean
   else:
       init_sampler = posterior_mean

   return (init_sampler)
## ******************************


## ********** Initialise from the last sample of another chain
def initialise_from_run(samples):
    ret = samples[samples.shape[0]-1,:]
    return (ret)
## ******************************


## ********** Optimise HMC
def optimise_hmc(dataset, m_mcmc, init_sampler, OPTION_SEED=-9999):

   def f(x):
      ret = [-a for a in m_mcmc._objective_grads(x)]
      #print ret[0]
      return ret

   print "Optimising epsilon and Lmax in HMC ..."
   _, optimised_settings_hmc, __ = aa_scheme.AHMC_lessL(f, init_sampler, 20, 50, epsilon_bounds=[1e-6, 1e0], L_bounds=[2,40], grid_res=20, verbose=False, criterion = "ESJD")
   print "done\n"

   epsilon = np.exp(optimised_settings_hmc[0])
   Lmax = optimised_settings_hmc[1]

   return (epsilon, Lmax)
## ******************************


## ********** Run HMC
def run_hmc(dataset, m_mcmc, optimised_settings_hmc, init_sampler, OPTION_NZ, OPTION_ARD, OPTION_SEED=-9999, DIR_RESULTS="./tmp_results", save=False, reduce_epsilon=False):
    def f(x):
        ret = [-a for a in m_mcmc._objective_grads(x)]
        #print ret[0]
        return ret

    if (OPTION_SEED == -9999):
        nsamples = 4 * m_mcmc.optimizer_array.size
    else:
        nsamples = 5000

    epsilon = optimised_settings_hmc[0]
    if(reduce_epsilon == True):
        epsilon = epsilon * 0.05
        nsamples = 100

    Lmax = optimised_settings_hmc[1]
    
    print "Running HMC ..."
    start_time = time()
    samples, accepts = aa_scheme.HMC_safe(f, num_samples=nsamples, epsilon=epsilon, Lmax=Lmax, x0=init_sampler)
    finish_time = time()
    print "Avg acceptance rate", accepts, "\n\n"
    
    if(save == True):
        if not os.path.exists(DIR_RESULTS):
            os.makedirs(DIR_RESULTS)

        filename_samples = DIR_RESULTS + "SAMPLES_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_HMC_SEED_" + `OPTION_SEED` + ".txt"
        filename_accepts = DIR_RESULTS + "ACCEPTS_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_HMC_SEED_" + `OPTION_SEED` + ".txt"
        filename_epsilon = DIR_RESULTS + "EPSILON_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_HMC_SEED_" + `OPTION_SEED` + ".txt"
        filename_Lmax = DIR_RESULTS + "LMAX_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_HMC_SEED_" + `OPTION_SEED` + ".txt"
        filename_time = DIR_RESULTS + "TIME_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_HMC_SEED_" + `OPTION_SEED` + ".txt"

        f=open(filename_samples,'ab'); np.savetxt(f, samples)
        f=open(filename_accepts,'ab'); np.savetxt(f, [accepts])
        f=open(filename_epsilon,'ab'); np.savetxt(f, [epsilon])
        f=open(filename_Lmax,'ab'); np.savetxt(f, [Lmax])
        f=open(filename_time,'ab'); np.savetxt(f, [finish_time-start_time])
    else:
        return samples

    if (OPTION_SEED != -9999):
        filename = DIR_RESULTS + "pkl_random_state_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_HMC_SEED_" + `OPTION_SEED` + ".pkl"
        out = open(filename, "wb", -1)
        random_state = np.random.get_state()
        pickle.dump(random_state, out)
## ******************************


## ********** Optimise Ancillary Augmentation scheme
def optimise_aa_scheme(dataset, m_mcmc, init_sampler, OPTION_SEED=-9999):

   def f(x):
      ret = [-a for a in m_mcmc._objective_grads(x)]
      #print ret[0]
      return ret

   print "Optimising epsilon, Lmax, and alpha in AA scheme ..."
   _, optimised_settings_aa, __ = aa_scheme.AAA(f, m_mcmc, kernel_priors, init_sampler, 20, 50, epsilon_bounds=[1e-4, 1e0], L_bounds=[2,40], alpha_bounds=[1e-4, 1e0], grid_res=8, verbose=False, criterion = "ESJD")
   print "done\n"

   epsilon = np.exp(optimised_settings_aa[0])
   Lmax = optimised_settings_aa[1]
   alpha = np.exp(optimised_settings_aa[2])

   return (epsilon, Lmax, alpha)
## ******************************


## ********** Run Ancillary Augmentation scheme
def run_aa_scheme(dataset, m_mcmc, optimised_settings_mcmc, init_sampler, OPTION_NZ, OPTION_ARD, OPTION_SEED=-9999, DIR_RESULTS="./tmp_results", save=False, reduce_epsilon=False):
    def f(x):
        ret = [-a for a in m_mcmc._objective_grads(x)]
        #print ret[0]
        return ret

    if (OPTION_SEED == -9999):
        nsamples = 4 * m_mcmc.optimizer_array.size
    else:
        nsamples = 5000

    epsilon = optimised_settings_mcmc[0]
    if(reduce_epsilon == True):
        epsilon = epsilon * 0.05
        nsamples = 100
    Lmax = optimised_settings_mcmc[1]
    alpha = optimised_settings_mcmc[2]
    
    print "Running AA scheme ..."
    start_time = time()
    samples, accepts = aa_scheme.AA(f, num_samples=nsamples, epsilon=epsilon, Lmax=Lmax, alpha=alpha, x0=init_sampler, m_mcmc=m_mcmc, kernel_priors=kernel_priors)
    finish_time = time()
    print "Avg acceptance rate", accepts, "\n\n"
    
    if(save == True):
        if not os.path.exists(DIR_RESULTS):
            os.makedirs(DIR_RESULTS)

        filename_samples = DIR_RESULTS + "SAMPLES_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_AA_SEED_" + `OPTION_SEED` + ".txt"
        filename_accepts = DIR_RESULTS + "ACCEPTS_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_AA_SEED_" + `OPTION_SEED` + ".txt"
        filename_epsilon = DIR_RESULTS + "EPSILON_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_AA_SEED_" + `OPTION_SEED` + ".txt"
        filename_Lmax = DIR_RESULTS + "LMAX_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_AA_SEED_" + `OPTION_SEED` + ".txt"
        filename_alpha = DIR_RESULTS + "ALPHA_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_AA_SEED_" + `OPTION_SEED` + ".txt"
        filename_time = DIR_RESULTS + "TIME_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_AA_SEED_" + `OPTION_SEED` + ".txt"

        f=open(filename_samples,'ab'); np.savetxt(f, samples)
        f=open(filename_accepts,'ab'); np.savetxt(f, [accepts])
        f=open(filename_epsilon,'ab'); np.savetxt(f, [epsilon])
        f=open(filename_Lmax,'ab'); np.savetxt(f, [Lmax])
        f=open(filename_alpha,'ab'); np.savetxt(f, [alpha])
        f=open(filename_time,'ab'); np.savetxt(f, [finish_time-start_time])

    else:
        return samples

    if (OPTION_SEED != -9999):
        filename = DIR_RESULTS + "pkl_random_state_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_AA_SEED_" + `OPTION_SEED` + ".pkl"
        out = open(filename, "wb", -1)
        random_state = np.random.get_state()
        pickle.dump(random_state, out)

## ******************************

class Experiment_prepare:
    def __init__(self, OPTION_NZ, OPTION_ARD, OPTION_SAMPLER):
        dataset = load_data()
        
        likelihood = Multiclass()
        likelihood.delta.fix(1e-3)
        
        kernel = setup_kernel(dataset, OPTION_ARD)

        Z = select_Z(dataset, OPTION_NZ)

        m_approx = optimise_GP_classifier(dataset, Z, likelihood, kernel, OPTION_ARD)
        
        m_mcmc = setup_mcmc_structure(dataset, Z, likelihood, kernel)

        gaussian_approx_post = compute_gaussian_approx_post_MAP(m_approx, m_mcmc)

        DIR_RESULTS = "RESULTS_" + "JOINT" + "_" + "ZOPTIM" + "/"
        if not os.path.exists(DIR_RESULTS):
            os.makedirs(DIR_RESULTS)
        
        filename = DIR_RESULTS + "pkl_gaussian_approx_post_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_" + ["HMC", "AA"][OPTION_SAMPLER] + ".pkl"
        out = open(filename, "wb")
        pickle.dump(gaussian_approx_post, out, -1)

        filename = DIR_RESULTS + "pkl_m_mcmc_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_" + ["HMC", "AA"][OPTION_SAMPLER] + ".pkl"
        out = open(filename, "wb")
        pickle.dump(m_mcmc, out, -1)


class Experiment_convergence_mcmc_joint:
    def __init__(self, OPTION_NZ, OPTION_ARD, OPTION_SAMPLER, OPTION_SEED):
        dataset = load_data()
        
        DIR_RESULTS = "RESULTS_" + "JOINT" + "_" + "ZOPTIM" + "/"

        filename = DIR_RESULTS + "pkl_gaussian_approx_post_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_" + ["HMC", "AA"][OPTION_SAMPLER] + ".pkl"
        out = open(filename, "rb")
        gaussian_approx_post = pickle.load(out)

        filename = DIR_RESULTS + "pkl_m_mcmc_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_" + ["HMC", "AA"][OPTION_SAMPLER] + ".pkl"
        out = open(filename, "rb")
        m_mcmc = pickle.load(out)

        m_mcmc.likelihood.unset_priors()

        if(OPTION_SAMPLER == 0):
            optimise_mcmc = optimise_hmc
            run_mcmc = run_hmc

        if(OPTION_SAMPLER == 1):
            optimise_mcmc = optimise_aa_scheme
            run_mcmc = run_aa_scheme

        filename = DIR_RESULTS + "pkl_random_state_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_" + ["HMC", "AA"][OPTION_SAMPLER] + "_SEED_" + `OPTION_SEED` + ".pkl"
        if os.path.exists(filename):
            print ("Continuing from previous run")
            out = open(filename, "rb")
            random_state = pickle.load(out)
            np.random.set_state(random_state)

            filename_samples = DIR_RESULTS + "SAMPLES_NZ_" + `OPTION_NZ` + "_ARD_" + `OPTION_ARD` + "_" + ["HMC", "AA"][OPTION_SAMPLER] + "_SEED_" + `OPTION_SEED` + ".txt"
            import subprocess
            tmp = subprocess.check_output("tail -1 " + filename_samples, shell=True)
            init_sampler = np.asfarray(tmp.split())

        else:
            print ("Running from scratch")

            init_sampler = initialise_from_approx(gaussian_approx_post, OPTION_SEED)
            optimised_settings_mcmc = optimise_mcmc(dataset, m_mcmc, init_sampler, OPTION_SEED)

        run_mcmc(dataset, m_mcmc, optimised_settings_mcmc, init_sampler, OPTION_NZ, OPTION_ARD, OPTION_SEED, DIR_RESULTS, save=True)

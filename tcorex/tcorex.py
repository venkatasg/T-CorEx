""" T-CorEx - Linear Time Covariance Estimation method based on Linear CorEx.
Given time-series data returns the covariance matrix for each time period.
More details coming soon ...
Code below written by:
Hrayr Harutyunyan (harhro@gmail.edu) and Greg Ver Steeg (gregv@isi.edu).
"""

from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import numpy as np
from scipy.stats import norm, rankdata
import time
import random
import torch


def g(x, t=4):
    """A transformation that suppresses outliers for a standard normal."""
    xp = np.clip(x, -t, t)
    diff = np.tanh(x - xp)
    return xp + diff


def g_inv(x, t=4):
    """Inverse of g transform."""
    xp = np.clip(x, -t, t)
    diff = np.arctanh(np.clip(x - xp, -1 + 1e-10, 1 - 1e-10))
    return xp + diff


def mean_impute(x, v):
    """Missing values in the data, x, are indicated by v. Wherever this value appears in x, it is replaced by the
    mean value taken from the marginal distribution of that column."""
    if not np.isnan(v):
        x = np.where(x == v, np.nan, x)
    x_new = []
    n_obs = []
    for i, xi in enumerate(x.T):
        missing_locs = np.where(np.isnan(xi))[0]
        xi_nm = xi[np.isfinite(xi)]
        xi[missing_locs] = np.mean(xi_nm)
        x_new.append(xi)
        n_obs.append(len(xi_nm))
    return np.array(x_new).T, np.array(n_obs)


def get_w_from_corex(corex):
    u = corex.ws
    z2 = corex.moments['Y_j^2']
    return u * np.sqrt(z2).reshape((-1, 1))


def entropy(x):
    return -(x * torch.log(x)).sum(dim=0)


class Corex:
    def __init__(self, nv, n_hidden=10, max_iter=10000, tol=1e-5, anneal=True, missing_values=None, update_iter=15,
                 gaussianize='standard', y_scale=1.0, l1=0.0, verbose=0, torch_device='cpu'):

        self.nv = nv  # Number of variables
        self.m = n_hidden  # Number of latent factors to learn
        self.max_iter = max_iter  # Number of iterations to try
        self.tol = tol  # Threshold for convergence
        self.anneal = anneal
        self.eps = 0  # If anneal is True, it's adjusted during optimization to avoid local minima
        self.missing_values = missing_values
        self.update_iter = update_iter  # Compute statistics every update_iter

        self.gaussianize = gaussianize  # Preprocess data: 'standard' scales to zero mean and unit variance

        self.y_scale = y_scale  # Can be arbitrary, but sets the scale of Y
        self.l1 = l1  # L1 on W regularization coefficient
        self.verbose = verbose
        if verbose:
            np.set_printoptions(precision=3, suppress=True, linewidth=160)
            print('Linear CorEx with {:d} latent factors'.format(n_hidden))

        self.torch_device = torch_device
        self.device = torch.device(torch_device)

        # define the weights of the model
        self.ws = np.random.normal(loc=0, scale=1.0 / np.sqrt(self.nv), size=(self.m, self.nv))
        self.ws = torch.tensor(self.ws, dtype=torch.float, device=self.device, requires_grad=True)
        # TODO: decide whether to use orthogonal initialization; do it for T-CorEx too
        # self.ws = torch.zeros((self.m, self.nv), dtype=torch.float, device=self.device, requires_grad=True)
        # self.ws = torch.nn.init.orthogonal_(self.ws, gain=1.0/np.sqrt(self.nv))
        self.transfer_weights()

    def transfer_weights(self):
        if self.torch_device == 'cpu':
            self.weights = self.ws.data.numpy()
        else:
            self.weights = self.ws.data.cpu().numpy()

    def forward(self, x_wno, anneal_eps):
        x_wno = torch.tensor(x_wno, dtype=torch.float, device=self.device)
        anneal_eps = torch.tensor(anneal_eps, dtype=torch.float, device=self.device)

        ns = x_wno.shape[0]
        anneal_noise = torch.randn((ns, self.nv), dtype=torch.float, device=self.device)
        x = torch.sqrt(1 - anneal_eps ** 2) * x_wno + anneal_eps * anneal_noise
        z_noise = self.y_scale * torch.randn((ns, self.m), dtype=torch.float, device=self.device)
        z_mean = torch.mm(x, self.ws.t())
        z = z_mean + z_noise

        epsilon = 1e-8
        z2 = (z ** 2).mean(dim=0)  # (m,)
        R = torch.mm(z.t(), x) / ns  # m, nv
        R = R / torch.sqrt(z2).reshape((self.m, 1))  # as <x^2_i> == 1 we don't divide by it
        ri = ((R ** 2) / torch.clamp(1 - R ** 2, epsilon, 1 - epsilon)).sum(dim=0)  # (nv,)

        # v_xi | z conditional mean
        outer_term = (1 / (1 + ri)).reshape((1, self.nv))
        inner_term_1 = R / torch.clamp(1 - R ** 2, epsilon, 1) / torch.sqrt(z2).reshape((self.m, 1))  # (m, nv)
        inner_term_2 = z  # (ns, m)
        cond_mean = outer_term * torch.mm(inner_term_2, inner_term_1)  # (ns, nv)

        # objective
        obj_part_1 = 0.5 * torch.log(torch.clamp(((x - cond_mean) ** 2).mean(dim=0), epsilon, np.inf)).sum(dim=0)
        obj_part_2 = 0.5 * torch.log(z2).sum(dim=0)
        reg_obj = torch.tensor(0, dtype=torch.float, device=self.device)
        if self.l1 > 0:
            reg_obj = torch.sum(self.l1 * torch.abs(self.ws))

        self.obj = obj_part_1 + obj_part_2 + reg_obj

        return {'obj': self.obj}

    def fit(self, x):
        x = np.asarray(x, dtype=np.float32)
        x = self.preprocess(x, fit=True)  # Fit a transform for each marginal
        assert x.shape[1] == self.nv
        self.x_std = x  # to have access to the standardized version of input

        anneal_schedule = [0.]
        if self.anneal:
            anneal_schedule = [0.6 ** k for k in range(1, 7)] + [0]

        # set up the optimizer
        optimizer = torch.optim.Adam([self.ws])

        for i_eps, eps in enumerate(anneal_schedule):
            start_time = time.time()
            self.eps = eps
            self.moments = self._calculate_moments(x, self.weights, quick=True)
            self._update_u(x)

            for i_loop in range(self.max_iter):
                # TODO: write a stopping condition
                if self.verbose:
                    print("annealing eps: {}, iter: {} / {}".format(eps, i_loop, self.max_iter), end='\r')

                ret = self.forward(x, eps)
                obj = ret['obj']

                optimizer.zero_grad()
                obj.backward()
                optimizer.step()
                self.transfer_weights()

                if self.verbose > 1 and i_loop % self.update_iter == 0:
                    self.moments = self._calculate_moments(x, self.weights, quick=True)
                    self._update_u(x)
                    print("tc = {}, obj = {}, eps = {}".format(self.tc, obj, eps))
            print("Annealing iteration finished, time = {}".format(time.time() - start_time))

        self.moments = self._calculate_moments(x, self.weights, quick=False)
        order = np.argsort(-self.moments["TCs"])  # Largest TC components first.
        self.weights = self.weights[order]
        self.ws.data = torch.tensor(self.weights, dtype=torch.float, device=self.device)
        self._update_u(x)
        self.moments = self._calculate_moments(x, self.weights, quick=False)

        # clear cache to free some GPU memory
        if self.torch_device == 'cuda':
            torch.cuda.empty_cache()

        return self

    def _update_u(self, x):
        self.us = self.getus(self.weights, x)

    @property
    def tc(self):
        """This actually returns the lower bound on TC that is optimized. The lower bound assumes a constraint that
         would be satisfied by a non-overlapping model.
         Check "moments" for two other estimates of TC that may be useful."""
        return self.moments["TC"]

    @property
    def tcs(self):
        """TCs for each individual latent factor. They DO NOT sum to TC in this case, because of overlaps."""
        return self.moments["TCs"]

    @property
    def mis(self):
        return - 0.5 * np.log1p(-self.moments["rho"] ** 2)

    def clusters(self):
        return np.argmax(np.abs(self.us), axis=0)

    def _sig(self, x, u):
        """Multiple the matrix u by the covariance matrix of x. We are interested in situations where
        n_variables >> n_samples, so we do this without explicitly constructing the covariance matrix."""
        n_samples = x.shape[0]
        y = x.dot(u.T)
        tmp_dot = x.T.dot(y)
        prod = (1 - self.eps ** 2) * tmp_dot.T / n_samples + self.eps ** 2 * u  # nv by m,  <X_i Y_j> / std Y_j
        return prod

    def getus(self, w, x):
        # U_{ji} = \frac{W_{ji}}{\sqrt{E[Z_j^2]}}
        """
        U = np.zeros(W.shape)
        for j in range(W.shape[0]):
            U[j, :] = W[j, :] / np.sqrt(self.z2_fromW(j, W, x))
        return U
        """
        tmp_dot = np.dot(self._sig(x, w), w.T)
        z2 = self.y_scale ** 2 + np.einsum("ii->i", tmp_dot)
        return w / np.sqrt(z2).reshape((-1, 1))

    def _calculate_moments(self, x, ws, quick=False):
        us = self.getus(ws, x)
        return self._calculate_moments_ns(x, us, quick=quick)

    def _calculate_moments_ns(self, x, ws, quick=False):
        """Calculate moments based on the weights and samples. We also calculate and save MI, TC, additivity, and
        the value of the objective. Note it is assumed that <X_i^2> = 1! """
        n_samples = x.shape[0]
        m = {}  # Dictionary of moments
        y = x.dot(ws.T)
        tmp_sum = np.einsum('lj,lj->j', y, y)
        m["uj"] = (1 - self.eps ** 2) * tmp_sum / n_samples + self.eps ** 2 * np.sum(ws ** 2, axis=1)

        if np.max(m["uj"]) > 1.0:
            print(np.max(m["uj"]))
            assert False

        tmp_dot = x.T.dot(y)
        m["rho"] = (1 - self.eps ** 2) * tmp_dot.T / n_samples + self.eps ** 2 * ws  # m by nv
        m["ry"] = ws.dot(m["rho"].T)  # normalized covariance of Y
        m["Y_j^2"] = self.y_scale ** 2 / (1. - m["uj"])
        np.fill_diagonal(m["ry"], 1)
        m["invrho"] = 1. / (1. - m["rho"] ** 2)
        m["rhoinvrho"] = m["rho"] * m["invrho"]
        m["Qij"] = np.dot(m['ry'], m["rhoinvrho"])
        m["Qi"] = np.einsum('ki,ki->i', m["rhoinvrho"], m["Qij"])
        # m["Qi-Si^2"] = np.einsum('ki,ki->i', m["rhoinvrho"], m["Qij"])
        m["Si"] = np.sum(m["rho"] * m["rhoinvrho"], axis=0)

        # This is the objective, a lower bound for TC
        m["TC"] = np.sum(np.log(1 + m["Si"])) \
                  - 0.5 * np.sum(np.log(1 - m["Si"] ** 2 + m["Qi"])) \
                  + 0.5 * np.sum(np.log(1 - m["uj"]))

        if not quick:
            m["MI"] = - 0.5 * np.log1p(-m["rho"] ** 2)
            m["X_i Y_j"] = m["rho"].T * np.sqrt(m["Y_j^2"])
            m["X_i Z_j"] = np.linalg.solve(m["ry"], m["rho"]).T
            m["X_i^2 | Y"] = (1. - np.einsum('ij,ji->i', m["X_i Z_j"], m["rho"])).clip(1e-6)
            m['I(Y_j ; X)'] = 0.5 * np.log(m["Y_j^2"]) - 0.5 * np.log(self.y_scale ** 2)
            m['I(X_i ; Y)'] = - 0.5 * np.log(m["X_i^2 | Y"])
            m["TCs"] = m["MI"].sum(axis=1) - m['I(Y_j ; X)']
            m["TC_no_overlap"] = m["MI"].max(axis=0).sum() - m[
                'I(Y_j ; X)'].sum()  # A direct calculation of TC where each variable is in exactly one group.
            m["TC_direct"] = m['I(X_i ; Y)'].sum() - m[
                'I(Y_j ; X)']  # A direct calculation of TC. Should be upper bound for "TC", "TC_no_overlap"
            m["additivity"] = (m["MI"].sum(axis=0) - m['I(X_i ; Y)']).sum()
        return m

    def transform(self, x, details=False):
        """Transform an array of inputs, x, into an array of k latent factors, Y.
            Optionally, you can get the remainder information and/or stop at a specified level."""
        x = self.preprocess(x)
        ns, nv = x.shape
        assert self.nv == nv, "Incorrect number of variables in input, %d instead of %d" % (nv, self.nv)
        if details:
            moments = self._calculate_moments(x, self.us)
            return x.dot(self.us.T), moments
        return x.dot(self.us.T)

    def preprocess(self, x, fit=False):
        """Transform each marginal to be as close to a standard Gaussian as possible.
        'standard' (default) just subtracts the mean and scales by the std.
        'empirical' does an empirical gaussianization (but this cannot be inverted).
        'outliers' tries to squeeze in the outliers
        Any other choice will skip the transformation."""
        if self.missing_values is not None:
            x, self.n_obs = mean_impute(x, self.missing_values)  # Creates a copy
        else:
            self.n_obs = len(x)
        if self.gaussianize == 'none':
            pass
        elif self.gaussianize == 'standard':
            if fit:
                mean = np.mean(x, axis=0)
                # std = np.std(x, axis=0, ddof=0).clip(1e-10)
                std = np.sqrt(np.sum((x - mean) ** 2, axis=0) / self.n_obs).clip(1e-10)
                self.theta = (mean, std)
            x = ((x - self.theta[0]) / self.theta[1])
            if np.max(np.abs(x)) > 6 and self.verbose:
                print("Warning: outliers more than 6 stds away from mean. Consider using gaussianize='outliers'")
        elif self.gaussianize == 'outliers':
            if fit:
                mean = np.mean(x, axis=0)
                std = np.std(x, axis=0, ddof=0).clip(1e-10)
                self.theta = (mean, std)
            x = g((x - self.theta[0]) / self.theta[1])  # g truncates long tails
        elif self.gaussianize == 'empirical':
            print("Warning: correct inversion/transform of empirical gauss transform not implemented.")
            x = np.array([norm.ppf((rankdata(x_i) - 0.5) / len(x_i)) for x_i in x.T]).T
        return x

    def invert(self, x):
        """Invert the preprocessing step to get x's in the original space."""
        if self.gaussianize == 'standard':
            return self.theta[1] * x + self.theta[0]
        elif self.gaussianize == 'outliers':
            return self.theta[1] * g_inv(x) + self.theta[0]
        else:
            return x

    def predict(self, y):
        return self.invert(np.dot(self.moments["X_i Z_j"], y.T).T)

    def get_covariance(self, normed=False):
        # This uses E(Xi|Y) formula for non-synergistic relationships
        m = self.moments
        z = m['rhoinvrho'] / (1 + m['Si'])
        cov = np.dot(z.T, z)
        cov /= (1. - self.eps ** 2)
        np.fill_diagonal(cov, 1)
        if normed:
            return cov
        else:
            return self.theta[1][:, np.newaxis] * self.theta[1] * cov


class TCorexBase(object):
    def __init__(self, nt, nv, n_hidden=10, max_iter=10000, tol=1e-5, anneal=True, missing_values=None,
                 gaussianize='standard', y_scale=1.0, update_iter=10,
                 pretrained_weights=None, verbose=0, torch_device='cpu'):

        self.nt = nt  # Number of timesteps
        self.nv = nv  # Number of variables
        self.m = n_hidden  # Number of latent factors to learn
        self.max_iter = max_iter  # Number of iterations to try
        self.tol = tol  # Threshold for convergence
        self.anneal = anneal
        self.eps = 0  # If anneal is True, it's adjusted during optimization to avoid local minima
        self.missing_values = missing_values

        self.gaussianize = gaussianize  # Preprocess data: 'standard' scales to zero mean and unit variance

        self.y_scale = y_scale  # Can be arbitrary, but sets the scale of Y
        self.update_iter = update_iter  # Compute statistics every update_iter
        self.pretrained_weights = pretrained_weights
        self.verbose = verbose
        if verbose:
            np.set_printoptions(precision=3, suppress=True, linewidth=160)
            print('Linear CorEx with {:d} latent factors'.format(n_hidden))

        self.torch_device = torch_device
        self.device = torch.device(torch_device)

    def forward(self, x_wno, anneal_eps, indices=None):
        raise NotImplementedError("forward function should be specified for all child classes")

    def _train_loop(self):
        """ train loop expects self have two variables x_input and x_std
        """
        if self.verbose:
            print("Starting the training loop ...")
        # set the annealing schedule
        anneal_schedule = [0.]
        if self.anneal:
            anneal_schedule = [0.6 ** k for k in range(1, 7)] + [0]

        # initialize the weights if pre-trained weights are specified
        if self.pretrained_weights is not None:
            self.ws = []
            for pre_w in self.pretrained_weights:
                self.ws.append(torch.tensor(pre_w, dtype=torch.float, device=self.device, requires_grad=True))
            self.transfer_weights()

        # set up the optimizer
        optimizer = torch.optim.Adam(self.ws + self.add_params)

        for eps in anneal_schedule:
            start_time = time.time()
            self.eps = eps  # for Greg's part of code
            for i_loop in range(self.max_iter):
                # TODO: write a stopping condition
                if self.verbose:
                    print("annealing eps: {}, iter: {} / {}".format(eps, i_loop, self.max_iter), end='\r')

                ret = self.forward(self.x_input, eps)
                obj = ret['total_obj']

                optimizer.zero_grad()
                obj.backward()
                optimizer.step()
                self.transfer_weights()

                main_obj = ret['main_obj']
                reg_obj = ret['reg_obj']
                if i_loop % self.update_iter == 0 and self.verbose > 1:
                    self.moments = self._calculate_moments(self.x_std, self.weights, quick=True)
                    self._update_u(self.x_std)
                    print("tc: {:.4f}, obj: {:.4f}, main: {:.4f}, reg: {:.4f}, eps: {:.4f}".format(
                        np.sum(self.tc), obj, main_obj, reg_obj, eps))
            print("Annealing iteration finished, time = {}".format(time.time() - start_time))

        self.moments = self._calculate_moments(self.x_std, self.weights, quick=False)  # Update moments with details
        self._update_u(self.x_std)

        # clear cache to free some GPU memory
        if self.torch_device == 'cuda':
            torch.cuda.empty_cache()

        return self

    def fit(self, x):
        x = [np.array(xt, dtype=np.float32) for xt in x]
        x = self.preprocess(x, fit=True)  # fit a transform for each marginal
        self.x_std = x  # to have an access to standardized x
        self.x_input = x  # to have an access to input
        return self._train_loop()

    def transfer_weights(self):
        if self.torch_device == 'cpu':
            self.weights = [w.data.numpy() for w in self.ws]
        else:
            self.weights = [w.data.cpu().numpy() for w in self.ws]

    def _update_u(self, x):
        self.us = [self.getus(w, xt) for w, xt in zip(self.weights, x)]

    @property
    def tc(self):
        """This actually returns the lower bound on TC that is optimized. The lower bound assumes a constraint that
         would be satisfied by a non-overlapping model.
         Check "moments" for two other estimates of TC that may be useful."""
        return [m["TC"] for m in self.moments]

    @property
    def tcs(self):
        """TCs for each individual latent factor. They DO NOT sum to TC in this case, because of overlaps."""
        return [m["TCs"] for m in self.moments]

    @property
    def mis(self):
        return [-0.5 * np.log1p(-m["rho"] ** 2) for m in self.moments]

    def clusters(self):
        return [np.argmax(np.abs(u), axis=0) for u in self.us]

    def _sig(self, x, u):
        """Multiple the matrix u by the covariance matrix of x. We are interested in situations where
        n_variables >> n_samples, so we do this without explicitly constructing the covariance matrix."""
        n_samples = x.shape[0]
        y = x.dot(u.T)
        tmp_dot = x.T.dot(y)
        prod = (1 - self.eps ** 2) * tmp_dot.T / n_samples + self.eps ** 2 * u  # nv by m,  <X_i Y_j> / std Y_j
        return prod

    def getus(self, w, x):
        # U_{ji} = \frac{W_{ji}}{\sqrt{E[Z_j^2]}}
        """
        U = np.zeros(W.shape)
        for j in range(W.shape[0]):
            U[j, :] = W[j, :] / np.sqrt(self.z2_fromW(j, W, x))
        return U
        """
        tmp_dot = np.dot(self._sig(x, w), w.T)
        z2 = self.y_scale ** 2 + np.einsum("ii->i", tmp_dot)
        return w / np.sqrt(z2).reshape((-1, 1))

    def _calculate_moments(self, x, ws, quick=False):
        us = [self.getus(w, xt) for w, xt in zip(ws, x)]
        ret = [None] * self.nt
        for t in range(self.nt):
            ret[t] = self._calculate_moments_ns(x[t], us[t], quick=quick)
        return ret

    def _calculate_moments_ns(self, x, ws, quick=False):
        """Calculate moments based on the weights and samples. We also calculate and save MI, TC, additivity, and
        the value of the objective. Note it is assumed that <X_i^2> = 1! """
        m = {}  # Dictionary of moments
        n_samples = x.shape[0]
        y = x.dot(ws.T)
        tmp_sum = np.einsum('lj,lj->j', y, y)
        m["uj"] = (1 - self.eps ** 2) * tmp_sum / n_samples + self.eps ** 2 * np.sum(ws ** 2, axis=1)

        tmp_dot = x.T.dot(y)
        m["rho"] = (1 - self.eps ** 2) * tmp_dot.T / n_samples + self.eps ** 2 * ws  # m by nv
        m["ry"] = ws.dot(m["rho"].T)  # normalized covariance of Y
        m["Y_j^2"] = self.y_scale ** 2 / (1. - m["uj"])
        np.fill_diagonal(m["ry"], 1)
        m["invrho"] = 1. / (1. - m["rho"] ** 2)
        m["rhoinvrho"] = m["rho"] * m["invrho"]
        m["Qij"] = np.dot(m['ry'], m["rhoinvrho"])
        m["Qi"] = np.einsum('ki,ki->i', m["rhoinvrho"], m["Qij"])
        # m["Qi-Si^2"] = np.einsum('ki,ki->i', m["rhoinvrho"], m["Qij"])
        m["Si"] = np.sum(m["rho"] * m["rhoinvrho"], axis=0)

        # This is the objective, a lower bound for TC
        m["TC"] = np.sum(np.log(1 + m["Si"])) \
                  - 0.5 * np.sum(np.log(1 - m["Si"] ** 2 + m["Qi"])) \
                  + 0.5 * np.sum(np.log(1 - m["uj"]))

        if not quick:
            m["MI"] = - 0.5 * np.log1p(-m["rho"] ** 2)
            m["X_i Y_j"] = m["rho"].T * np.sqrt(m["Y_j^2"])
            m["X_i Z_j"] = np.linalg.solve(m["ry"], m["rho"]).T
            m["X_i^2 | Y"] = (1. - np.einsum('ij,ji->i', m["X_i Z_j"], m["rho"])).clip(1e-6)
            m['I(Y_j ; X)'] = 0.5 * np.log(m["Y_j^2"]) - 0.5 * np.log(self.y_scale ** 2)
            m['I(X_i ; Y)'] = - 0.5 * np.log(m["X_i^2 | Y"])
            m["TCs"] = m["MI"].sum(axis=1) - m['I(Y_j ; X)']
            m["TC_no_overlap"] = m["MI"].max(axis=0).sum() - m[
                'I(Y_j ; X)'].sum()  # A direct calculation of TC where each variable is in exactly one group.
            m["TC_direct"] = m['I(X_i ; Y)'].sum() - m[
                'I(Y_j ; X)']  # A direct calculation of TC. Should be upper bound for "TC", "TC_no_overlap"
            m["additivity"] = (m["MI"].sum(axis=0) - m['I(X_i ; Y)']).sum()
        return m

    def transform(self, x, details=False):
        """Transform an array of inputs, x, into an array of k latent factors, Y.
            Optionally, you can get the remainder information and/or stop at a specified level."""
        x = self.preprocess(x)
        ret = [a.dot(w.T) for (a, w) in zip(x, self.weights)]
        if details:
            moments = self._calculate_moments(x, self.us)
            return ret, moments
        return ret

    def preprocess(self, X, fit=False):
        """Transform each marginal to be as close to a standard Gaussian as possible.
        'standard' (default) just subtracts the mean and scales by the std.
        'empirical' does an empirical gaussianization (but this cannot be inverted).
        'outliers' tries to squeeze in the outliers
        Any other choice will skip the transformation."""
        warnings = []
        ret = [None] * len(X)
        if fit:
            self.theta = []
        for t in range(len(X)):
            x = X[t]
            if self.missing_values is not None:
                x, n_obs = mean_impute(x, self.missing_values)  # Creates a copy
            else:
                n_obs = len(x)
            if self.gaussianize == 'none':
                pass
            elif self.gaussianize == 'standard':
                if fit:
                    mean = np.mean(x, axis=0)
                    # std = np.std(x, axis=0, ddof=0).clip(1e-10)
                    std = np.sqrt(np.sum((x - mean) ** 2, axis=0) / n_obs).clip(1e-10)
                    self.theta.append((mean, std))
                x = ((x - self.theta[t][0]) / self.theta[t][1])
                if np.max(np.abs(x)) > 6 and self.verbose:
                    warnings.append("Warning: outliers more than 6 stds away from mean. "
                                    "Consider using gaussianize='outliers'")
            elif self.gaussianize == 'outliers':
                if fit:
                    mean = np.mean(x, axis=0)
                    std = np.std(x, axis=0, ddof=0).clip(1e-10)
                    self.theta.append((mean, std))
                x = g((x - self.theta[t][0]) / self.theta[t][1])  # g truncates long tails
            elif self.gaussianize == 'empirical':
                warnings.append("Warning: correct inversion/transform of empirical gauss transform not implemented.")
                x = np.array([norm.ppf((rankdata(x_i) - 0.5) / len(x_i)) for x_i in x.T]).T
            ret[t] = x
        for w in set(warnings):
            print(w)
        return ret

    def invert(self, x):
        # TODO: consider timesteps
        """Invert the preprocessing step to get x's in the original space."""
        if self.gaussianize == 'standard':
            return self.theta[1] * x + self.theta[0]
        elif self.gaussianize == 'outliers':
            return self.theta[1] * g_inv(x) + self.theta[0]
        else:
            return x

    def predict(self, y):
        # NOTE: not sure what does this function do
        ret = [None] * self.nt
        for t in range(self.nt):
            ret[t] = self.invert(np.dot(self.moments[t]["X_i Z_j"], y[t].T).T)
        return ret

    def get_covariance(self, indices=None, normed=False):
        if indices is None:
            indices = range(self.nt)
        cov = self.forward(self.x_input, anneal_eps=0, indices=indices)['sigma']
        for t in range(self.nt):
            if cov[t] is None:
                continue
            if self.torch_device == 'cpu':
                cov[t] = cov[t].data.numpy()
            else:
                cov[t] = cov[t].data.cpu().numpy()
            if not normed:
                cov[t] = self.theta[t][1][:, np.newaxis] * self.theta[t][1] * cov[t]
        if self.torch_device == 'cuda':
            torch.cuda.empty_cache()
        return cov


class TCorex(TCorexBase):
    def __init__(self, l1=0.0, l2=0.0, reg_type='W', init=True, gamma=0.5,
                 max_sample_cnt=2 ** 30, weighted_obj=False, **kwargs):
        """
        :param gamma: parameter that controls the decay of weights.
                      weight of a sample whose distance from the current time-step is d will have 1.0/(gamma^d).
                      If gamma is equal to 1 all samples will be used with weight 1.
        :parm max_sample_cnt: maximum number of samples to use. Setting this to smaller values will give some speed up.
        """
        super(TCorex, self).__init__(**kwargs)
        self.l1 = l1
        self.l2 = l2
        self.reg_type = reg_type
        self.init = init
        self.gamma = np.float(gamma)
        self.max_sample_count = max_sample_cnt
        self.weighted_obj = weighted_obj
        self.window_len = None  # this depends on x and will be computed in fit()

        # define the weights of the model
        if (not self.init) and (self.pretrained_weights is None):
            self.ws = []
            for t in range(self.nt):
                wt = np.random.normal(loc=0, scale=1.0 / np.sqrt(self.nv), size=(self.m, self.nv))
                wt = torch.tensor(wt, dtype=torch.float, device=self.device, requires_grad=True)
                self.ws.append(wt)
            self.transfer_weights()
        self.add_params = []  # used in _train_loop()

    def forward(self, x_wno, anneal_eps, indices=None):
        # copy x_wno
        x_wno = [xt.copy() for xt in x_wno]
        # add annealing noise
        for t in range(self.nt):
            ns = x_wno[t].shape[0]
            anneal_noise = np.random.normal(size=(ns, self.nv))
            x_wno[t] = np.sqrt(1 - anneal_eps ** 2) * x_wno[t] + anneal_eps * anneal_noise
        self.x = [torch.tensor(xt, dtype=torch.float, device=self.device) for xt in x_wno]

        self.z = [None] * self.nt
        for t in range(self.nt):
            ns = x_wno[t].shape[0]
            z_noise = self.y_scale * torch.randn((ns, self.m), dtype=torch.float, device=self.device)
            z_mean = torch.mm(self.x[t], self.ws[t].t())
            self.z[t] = z_mean + z_noise

        epsilon = 1e-8
        self.objs = [None] * self.nt
        self.sigma = [None] * self.nt
        mi_xz = [None] * self.nt

        # store all concatenations here for better memory usage
        concats = dict()

        for t in range(self.nt):
            l = max(0, t - self.window_len[t])
            r = min(self.nt, t + self.window_len[t] + 1)
            weights = []
            left_t = t
            right_t = t
            for i in range(l, r):
                cur_ns = x_wno[i].shape[0]
                coef = np.power(self.gamma, np.abs(i - t))
                # skip if the importance is too low
                if coef < 1e-6:
                    continue
                left_t = min(left_t, i)
                right_t = max(right_t, i)
                weights.append(torch.tensor(coef * np.ones((cur_ns,)), dtype=torch.float, device=self.device))

            weights = torch.cat(weights, dim=0)
            weights = weights / torch.sum(weights)
            weights = weights.reshape((-1, 1))

            t_range = (left_t, right_t)
            if t_range in concats:
                x_all = concats[t_range]
            else:
                x_all = torch.cat(self.x[left_t:right_t+1], dim=0)
                concats[t_range] = x_all
            ns_tot = x_all.shape[0]

            z_all_mean = torch.mm(x_all, self.ws[t].t())
            z_all_noise = self.y_scale * torch.randn((ns_tot, self.m), dtype=torch.float, device=self.device)
            z_all = z_all_mean + z_all_noise
            z2_all = ((z_all ** 2) * weights).sum(dim=0)  # (m,)
            R_all = torch.mm((z_all * weights).t(), x_all)  # m, nv
            R_all = R_all / torch.sqrt(z2_all).reshape((self.m, 1))  # as <x^2_i> == 1 we don't divide by it

            z2 = z2_all
            R = R_all

            if self.weighted_obj:
                X = x_all
                Z = z_all
            else:
                X = self.x[t]
                Z = self.z[t]

            if self.reg_type == 'MI':
                mi_xz[t] = -0.5 * torch.log1p(-torch.clamp(R ** 2, 0, 1 - epsilon))

            # the rest depends on R and z2 only
            ri = ((R ** 2) / torch.clamp(1 - R ** 2, epsilon, 1 - epsilon)).sum(dim=0)  # (nv,)

            # v_xi | z conditional mean
            outer_term = (1 / (1 + ri)).reshape((1, self.nv))
            inner_term_1 = R / torch.clamp(1 - R ** 2, epsilon, 1) / torch.sqrt(z2).reshape((self.m, 1))  # (m, nv)
            inner_term_2 = Z  # (ns, m)
            cond_mean = outer_term * torch.mm(inner_term_2, inner_term_1)  # (ns, nv)

            # calculate normed covariance matrix if needed
            if ((indices is not None) and (t in indices)) or self.reg_type == 'Sigma':
                inner_mat = 1.0 / (1 + ri).reshape((1, self.nv)) * R / torch.clamp(1 - R ** 2, epsilon, 1)
                self.sigma[t] = torch.mm(inner_mat.t(), inner_mat)
                identity_matrix = torch.eye(self.nv, dtype=torch.float, device=self.device)
                self.sigma[t] = self.sigma[t] * (1 - identity_matrix) + identity_matrix

            # objective
            if self.weighted_obj:
                obj_part_1 = 0.5 * torch.log(
                    torch.clamp((((X - cond_mean) ** 2) * weights).sum(dim=0), epsilon, np.inf)).sum(dim=0)
            else:
                obj_part_1 = 0.5 * torch.log(torch.clamp(((X - cond_mean) ** 2).mean(dim=0), epsilon, np.inf)).sum(
                    dim=0)
            obj_part_2 = 0.5 * torch.log(z2).sum(dim=0)
            self.objs[t] = obj_part_1 + obj_part_2

        # experiments show that main_obj scales approximately linearly with nv
        # also it is a sum of over time steps, so we divide on (nt * nv)
        self.main_obj = 1.0 / (self.nt * self.nv) * sum(self.objs)

        # regularization
        reg_matrices = [None] * self.nt
        if self.reg_type == 'W':
            reg_matrices = self.ws
        if self.reg_type == 'WWT':
            for t in range(self.nt):
                reg_matrices[t] = torch.mm(self.ws[t].t(), self.ws[t])
        if self.reg_type == 'Sigma':
            reg_matrices = self.sigma
        if self.reg_type == 'MI':
            reg_matrices = mi_xz

        self.reg_obj = torch.tensor(0.0, dtype=torch.float, device=self.device)

        # experiments show that L1 and L2 regularizations scale approximately linearly with nv
        if self.l1 > 0:
            l1_reg = sum([torch.abs(reg_matrices[t + 1] - reg_matrices[t]).sum() for t in range(self.nt - 1)])
            l1_reg = 1.0 / (self.nt * self.nv) * l1_reg
            self.reg_obj = self.reg_obj + self.l1 * l1_reg

        if self.l2 > 0:
            l2_reg = sum([((reg_matrices[t + 1] - reg_matrices[t]) ** 2).sum() for t in range(self.nt - 1)])
            l2_reg = 1.0 / (self.nt * self.nv) * l2_reg
            self.reg_obj = self.reg_obj + self.l2 * l2_reg

        self.total_obj = self.main_obj + self.reg_obj

        return {'total_obj': self.total_obj,
                'main_obj': self.main_obj,
                'reg_obj': self.reg_obj,
                'objs': self.objs,
                'sigma': self.sigma}

    def fit(self, x):
        # Compute the window lengths for each time step and define the model
        n_samples = [len(xt) for xt in x]
        window_len = [0] * self.nt
        for i in range(self.nt):
            k = 0
            while k <= self.nt and sum(n_samples[max(0, i - k):min(self.nt, i + k + 1)]) < self.max_sample_count:
                k += 1
            window_len[i] = k
        self.window_len = window_len

        # Use the priors to estimate <X_i> and std(x_i) better
        self.theta = [None] * self.nt
        for t in range(self.nt):
            sum_weights = 0.0
            l = max(0, t - window_len[t])
            r = min(self.nt, t + window_len[t] + 1)
            for i in range(l, r):
                w = np.power(self.gamma, np.abs(i - t))
                sum_weights += x[i].shape[0] * w

            mean_prior = np.zeros((self.nv,))
            for i in range(l, r):
                w = np.power(self.gamma, np.abs(i - t))
                for sample in x[i]:
                    mean_prior += w * np.array(sample)
            mean_prior /= sum_weights

            var_prior = np.zeros((self.nv,))
            for i in range(l, r):
                w = np.power(self.gamma, np.abs(i - t))
                for sample in x[i]:
                    var_prior += w * ((np.array(sample) - mean_prior) ** 2)
            var_prior /= sum_weights
            std_prior = np.sqrt(var_prior)

            self.theta[t] = (mean_prior, std_prior)

        x = self.preprocess(x, fit=False)  # standardize the data using the better estimates
        x = [np.array(xt, dtype=np.float32) for xt in x]  # convert to np.float32
        self.x_std = x  # to have an access to standardized x
        self.x_input = x  # to have an access to input

        # initialize weights using the weighs of the linear CorEx trained on all data
        if self.init:
            if self.verbose:
                print("Initializing with weights of a linear CorEx learned on whole data")
            init_start = time.time()
            lin_corex = Corex(nv=self.nv,
                              n_hidden=self.m,
                              max_iter=self.max_iter,
                              anneal=self.anneal,
                              verbose=self.verbose,
                              torch_device=self.torch_device,
                              gaussianize=self.gaussianize)
            # take maximum self.max_sample_cnt samples
            data_concat = np.concatenate(x, axis=0)
            if data_concat.shape[0] > self.max_sample_count:
                random.shuffle(data_concat)
                data_concat = data_concat[:self.max_sample_count]
            lin_corex.fit(data_concat)
            self.pretrained_weights = [lin_corex.weights] * self.nt
            if self.verbose:
                print("Initialization took {:.2f} seconds".format(time.time() - init_start))

        return self._train_loop()


class TCorexLearnable(TCorexBase):
    def __init__(self, l1=0.0, l2=0.0, entropy_lamb=0.2, reg_type='W',
                 init=True, max_sample_cnt=2 ** 30, weighted_obj=False, **kwargs):
        """
        :parm max_sample_cnt: maximum number of samples to use. Setting this to smaller values will give some speed up.
        """
        super(TCorexLearnable, self).__init__(**kwargs)
        self.l1 = l1
        self.l2 = l2
        self.entropy_lamb = entropy_lamb
        self.reg_type = reg_type
        self.init = init
        self.max_sample_count = max_sample_cnt
        self.weighted_obj = weighted_obj

        self.window_len = None  # this depends on x and will be computed in fit()
        self.sample_weights = None
        self.theta = [None] * self.nt

        # define the weights of the model
        if (not self.init) and (self.pretrained_weights is None):
            self.ws = []
            for t in range(self.nt):
                wt = np.random.normal(loc=0, scale=1.0 / np.sqrt(self.nv), size=(self.m, self.nv))
                wt = torch.tensor(wt, dtype=torch.float, device=self.device, requires_grad=True)
                self.ws.append(wt)
            self.transfer_weights()

    def forward(self, x_wno, anneal_eps, indices=None):
        x_wno = [torch.tensor(xt, dtype=torch.float, device=self.device) for xt in x_wno]

        # compute normalized sample weights
        norm_sample_weights = []
        for t in range(self.nt):
            l = max(0, t - self.window_len[t])
            r = min(self.nt, t + self.window_len[t] + 1)
            sw = []
            for i in range(l, r):
                cur_ns = x_wno[i].shape[0]
                sw.append(self.sample_weights[t][i-l] * torch.ones((cur_ns,), dtype=torch.float, device=self.device))
            sw = torch.cat(sw, dim=0)
            sw = sw.softmax(dim=0)
            sw = sw.reshape((-1, 1))
            norm_sample_weights.append(sw)

        # normalize x_wno
        means = []
        stds = []
        for t in range(self.nt):
            l = max(0, t - self.window_len[t])
            r = min(self.nt, t + self.window_len[t] + 1)
            x_part = torch.cat(x_wno[l:r], dim=0)
            weights = norm_sample_weights[t]
            mean = (weights * x_part).sum(dim=0)
            variance = (weights * (x_part - mean.reshape((1, self.nv))) ** 2).sum(dim=0)
            std = torch.sqrt(variance)
            means.append(mean)
            stds.append(std)

        for t in range(self.nt):
            x_wno[t] = (x_wno[t] - means[t].reshape((1, self.nv))) / stds[t].reshape((1, self.nv))
            if self.torch_device == 'cpu':
                self.theta[t] = (means[t].data.numpy(), stds[t].data.numpy())
            else:
                self.theta[t] = (means[t].data.cpu().numpy(), stds[t].data.cpu().numpy())

        # save normalized input
        self.x_std = []
        for t in range(self.nt):
            if self.torch_device == 'cpu':
                self.x_std.append(x_wno[t].data.numpy())
            else:
                self.x_std.append(x_wno[t].data.cpu().numpy())

        # add annealing noise
        anneal_eps = torch.tensor(anneal_eps, dtype=torch.float, device=self.device)
        for t in range(self.nt):
            ns = x_wno[t].shape[0]
            anneal_noise = torch.tensor(np.random.normal(size=(ns, self.nv)), dtype=torch.float, device=self.device)
            x_wno[t] = torch.sqrt(1 - anneal_eps ** 2) * x_wno[t] + anneal_eps * anneal_noise
        self.x = x_wno

        # compute z
        self.z = [None] * self.nt
        for t in range(self.nt):
            ns = x_wno[t].shape[0]
            z_noise = self.y_scale * torch.randn((ns, self.m), dtype=torch.float, device=self.device)
            z_mean = torch.mm(self.x[t], self.ws[t].t())
            self.z[t] = z_mean + z_noise

        epsilon = 1e-8
        self.objs = [None] * self.nt
        self.sigma = [None] * self.nt
        mi_xz = [None] * self.nt

        # store all concatenations here for better memory usage
        concats = dict()

        for t in range(self.nt):
            l = max(0, t - self.window_len[t])
            r = min(self.nt, t + self.window_len[t] + 1)
            weights = norm_sample_weights[t]

            t_range = (l, r)
            if t_range in concats:
                x_all = concats[t_range]
            else:
                x_all = torch.cat(self.x[l:r], dim=0)
                concats[t_range] = x_all
            ns_tot = x_all.shape[0]

            z_all_mean = torch.mm(x_all, self.ws[t].t())
            z_all_noise = self.y_scale * torch.randn((ns_tot, self.m), dtype=torch.float, device=self.device)
            z_all = z_all_mean + z_all_noise
            z2_all = ((z_all ** 2) * weights).sum(dim=0)  # (m,)
            R_all = torch.mm((z_all * weights).t(), x_all)  # m, nv
            R_all = R_all / torch.sqrt(z2_all).reshape((self.m, 1))  # as <x^2_i> == 1 we don't divide by it

            z2 = z2_all
            R = R_all

            if self.weighted_obj:
                X = x_all
                Z = z_all
            else:
                X = self.x[t]
                Z = self.z[t]

            if self.reg_type == 'MI':
                mi_xz[t] = -0.5 * torch.log1p(-torch.clamp(R ** 2, 0, 1 - epsilon))

            # the rest depends on R and z2 only
            ri = ((R ** 2) / torch.clamp(1 - R ** 2, epsilon, 1 - epsilon)).sum(dim=0)  # (nv,)

            # v_xi | z conditional mean
            outer_term = (1 / (1 + ri)).reshape((1, self.nv))
            inner_term_1 = R / torch.clamp(1 - R ** 2, epsilon, 1) / torch.sqrt(z2).reshape((self.m, 1))  # (m, nv)
            # NOTE: we use z[t], but seems only for objective not for the covariance estimate
            inner_term_2 = Z  # (ns, m)
            cond_mean = outer_term * torch.mm(inner_term_2, inner_term_1)  # (ns, nv)

            # calculate normed covariance matrix if needed
            if ((indices is not None) and (t in indices)) or self.reg_type == 'Sigma':
                inner_mat = 1.0 / (1 + ri).reshape((1, self.nv)) * R / torch.clamp(1 - R ** 2, epsilon, 1)
                self.sigma[t] = torch.mm(inner_mat.t(), inner_mat)
                identity_matrix = torch.eye(self.nv, dtype=torch.float, device=self.device)
                self.sigma[t] = self.sigma[t] * (1 - identity_matrix) + identity_matrix

            # objective
            if self.weighted_obj:
                obj_part_1 = 0.5 * torch.log(
                    torch.clamp((((X - cond_mean) ** 2) * weights).sum(dim=0), epsilon, np.inf)).sum(dim=0)
            else:
                obj_part_1 = 0.5 * torch.log(torch.clamp(((X - cond_mean) ** 2).mean(dim=0), epsilon, np.inf)).sum(
                    dim=0)
            obj_part_2 = 0.5 * torch.log(z2).sum(dim=0)
            self.objs[t] = obj_part_1 + obj_part_2

        # experiments show that main_obj scales approximately linearly with nv
        # also it is a sum of over time steps, so we divide on (nt * nv)
        self.main_obj = 1.0 / (self.nt * self.nv) * sum(self.objs)

        # regularization
        reg_matrices = [None] * self.nt
        if self.reg_type == 'W':
            reg_matrices = self.ws
        if self.reg_type == 'WWT':
            for t in range(self.nt):
                reg_matrices[t] = torch.mm(self.ws[t].t(), self.ws[t])
        if self.reg_type == 'Sigma':
            reg_matrices = self.sigma
        if self.reg_type == 'MI':
            reg_matrices = mi_xz

        self.reg_obj = torch.tensor(0.0, dtype=torch.float, device=self.device)

        # experiments show that L1 and L2 regularizations scale approximately linearly with nv
        if self.l1 > 0:
            l1_reg = sum([torch.abs(reg_matrices[t + 1] - reg_matrices[t]).sum() for t in range(self.nt - 1)])
            l1_reg = 1.0 / (self.nt * self.nv) * l1_reg
            self.reg_obj = self.reg_obj + self.l1 * l1_reg

        if self.l2 > 0:
            l2_reg = sum([((reg_matrices[t + 1] - reg_matrices[t]) ** 2).sum() for t in range(self.nt - 1)])
            l2_reg = 1.0 / (self.nt * self.nv) * l2_reg
            self.reg_obj = self.reg_obj + self.l2 * l2_reg

        # self.entropy_obj = sum([entropy(sw) for sw in norm_sample_weights])
        self.entropy_obj = -sum([torch.max(sw) for sw in norm_sample_weights])
        self.entropy_obj = -self.entropy_lamb * self.entropy_obj / self.nt

        self.total_obj = self.main_obj + self.reg_obj + self.entropy_obj

        return {'total_obj': self.total_obj,
                'main_obj': self.main_obj,
                'reg_obj': self.reg_obj,
                'entropy_obj': self.entropy_obj,
                'objs': self.objs,
                'sigma': self.sigma}

    def fit(self, x):
        # Compute the window lengths for each time step and define the model
        n_samples = [len(xt) for xt in x]
        window_len = [0] * self.nt
        for i in range(self.nt):
            k = 0
            while k <= self.nt and sum(n_samples[max(0, i - k):min(self.nt, i + k + 1)]) < self.max_sample_count:
                k += 1
            window_len[i] = k
        self.window_len = window_len

        # define sample weights
        self.sample_weights = []
        for t in range(self.nt):
            l = max(0, t - self.window_len[t])
            r = min(self.nt, t + self.window_len[t] + 1)
            self.sample_weights.append(torch.tensor(np.zeros((r - l,)), dtype=torch.float,
                                                    device=self.device, requires_grad=True))
        self.add_params = self.sample_weights  # used in _train_loop()

        x = [np.array(xt, dtype=np.float32) for xt in x]  # convert to np.float32
        self.x_input = x  # have an access to input

        # initialize weights using the weighs of the linear CorEx trained on all data
        if self.init:
            if self.verbose:
                print("Initializing with weights of a linear CorEx learned on whole data")
            init_start = time.time()
            lin_corex = Corex(nv=self.nv,
                              n_hidden=self.m,
                              max_iter=self.max_iter,
                              anneal=self.anneal,
                              verbose=self.verbose,
                              torch_device=self.torch_device,
                              gaussianize=self.gaussianize)
            # take maximum self.max_sample_cnt samples
            data_concat = np.concatenate(x, axis=0)
            if data_concat.shape[0] > self.max_sample_count:
                random.shuffle(data_concat)
                data_concat = data_concat[:self.max_sample_count]
            lin_corex.fit(data_concat)
            self.pretrained_weights = [lin_corex.weights] * self.nt
            if self.verbose:
                print("Initialization took {:.2f} seconds".format(time.time() - init_start))

        return self._train_loop()

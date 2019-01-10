#!/usr/bin/env python3

import time
from collections import OrderedDict
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import gpytorch
import numpy as np
from botorch.optim.numpy_converter import (
    TorchAttr,
    module_to_array,
    set_params_with_array,
)
from botorch.optim.outcome_constraints import ParameterBounds
from botorch.utils import check_convergence
from gpytorch.mlls.marginal_log_likelihood import MarginalLogLikelihood
from scipy.optimize import Bounds, minimize
from torch import Tensor
from torch.optim.adam import Adam
from torch.optim.optimizer import Optimizer


class OptimizationIteration(NamedTuple):
    itr: int
    fun: float
    time: float


def fit_torch(
    mll: MarginalLogLikelihood,
    optimizer_cls: Optimizer = Adam,
    lr: float = 0.05,
    maxiter: int = 100,
    optimizer_args: Optional[Dict[str, float]] = None,
    disp: bool = True,
    track_iterations: bool = True,
) -> Tuple[MarginalLogLikelihood, Optional[List[OptimizationIteration]]]:
    """Fit a gpytorch model by maximizing MLL with a torch optimizer.

    The model and likelihood in mll must already be in train mode.

    Args:
        mll: MarginalLogLikelihood to be maximized.
        optimizer_cls: Torch optimizer to use. Must not need a closure.
            Defaults to Adam.
        lr: Starting learning rate.
        maxiter: Maximum number of iterations.
        optimizer_args: Additional arguments to instantiate optimizer_cls.
        disp: Print information during optimization.
        track_iterations: Track the function values and wall time for each
            iteration.

    Returns:
        mll: mll with parameters optimized in-place.
        iterations: List of OptimizationIteration objects describing each
            iteration. If track_iterations is False, will be an empty list.
    """
    optimizer_args = {} if optimizer_args is None else optimizer_args
    optimizer = optimizer_cls(
        params=[{"params": mll.parameters()}], lr=lr, **optimizer_args
    )

    iterations = []
    t1 = time.time()

    param_trajectory: Dict[str, List[Tensor]] = {
        name: [] for name, param in mll.named_parameters()
    }
    loss_trajectory: List[float] = []
    i = 0
    converged = False
    train_inputs, train_targets = mll.model.train_inputs, mll.model.train_targets
    while not converged:
        optimizer.zero_grad()
        output = mll.model(*train_inputs)
        # we sum here to support batch mode
        loss = -mll(output, train_targets, *train_inputs).sum()
        loss.backward()
        loss_trajectory.append(loss.item())
        for name, param in mll.named_parameters():
            param_trajectory[name].append(param.detach().clone())
        if disp and (i % 10 == 0 or i == (maxiter - 1)):
            print(f"Iter {i +1}/{maxiter}: {loss.item()}")
        if track_iterations:
            iterations.append(OptimizationIteration(i, loss.item(), time.time() - t1))
        optimizer.step()
        i += 1
        converged = check_convergence(
            loss_trajectory=loss_trajectory,
            param_trajectory=param_trajectory,
            options={},
            max_iter=maxiter,
        )
    return mll, iterations if track_iterations else None


def fit_scipy(
    mll: MarginalLogLikelihood,
    bounds: Optional[ParameterBounds] = None,
    method: str = "L-BFGS-B",
    options: Optional[Dict[str, Any]] = None,
    track_iterations: bool = True,
    max_preconditioner_size: int = 10,
) -> Tuple[MarginalLogLikelihood, Optional[List[OptimizationIteration]]]:
    """Fit a gpytorch model by maximizing MLL with a scipy optimizer.

    The model and likelihood in mll must already be in train mode.

    Args:
        mll: MarginalLogLikelihood to be maximized.
        bounds: A ParameterBounds dictionary mapping parameter names to tuples of
            lower and upper bounds.
        method: Solver type, passed along to scipy.minimize.
        options: Dictionary of solver options, passed along to scipy.minimize.
        track_iterations: Track the function values and wall time for each
            iteration.
        max_preconditioner_size: Size of gpytorch preconditioner, for improving
            stability of the MLL estimate.

    Returns:
        mll: mll with parameters optimized in-place.
        iterations: List of OptimizationIteration objects describing each
            iteration. If track_iterations is False, will be an empty list.
    """
    x0, property_dict, bounds = module_to_array(module=mll, bounds=bounds)
    x0 = x0.astype(np.float64)
    if bounds is not None:
        bounds = Bounds(lb=bounds[0], ub=bounds[1], keep_feasible=True)

    xs = []
    ts = []
    t1 = time.time()

    def store_iteration(xk):
        xs.append(xk.copy())
        ts.append(time.time() - t1)

    cb = store_iteration if track_iterations else None

    res = minimize(
        _scipy_objective_and_grad,
        x0,
        args=(mll, property_dict, max_preconditioner_size),
        bounds=bounds,
        method=method,
        jac=True,
        options=options,
        callback=cb,
    )

    iterations = []
    if track_iterations:
        for i, xk in enumerate(xs):
            obj, _ = _scipy_objective_and_grad(
                xk, mll, property_dict, max_preconditioner_size
            )
            iterations.append(OptimizationIteration(i, obj, ts[i]))

    # Set to optimum
    mll = set_params_with_array(mll, res.x, property_dict)
    return mll, iterations


def _scipy_objective_and_grad(
    x: np.ndarray,
    mll: MarginalLogLikelihood,
    property_dict: Dict[str, TorchAttr],
    max_preconditioner_size: int,
) -> Tuple[float, np.ndarray]:
    mll = set_params_with_array(mll, x, property_dict)
    train_inputs, train_targets = mll.model.train_inputs, mll.model.train_targets
    mll.zero_grad()
    output = mll.model(*train_inputs)
    with gpytorch.settings.max_preconditioner_size(max_preconditioner_size):
        loss = -mll(output, train_targets, *train_inputs).sum()
    loss.backward()
    param_dict = OrderedDict(mll.named_parameters())
    grad = []
    for p_name in property_dict:
        t = param_dict[p_name].grad
        if t is None:
            # this deals with parameters that do not affect the loss
            grad.append(np.zeros(property_dict[p_name].shape.numel()))
        else:
            grad.append(t.detach().view(-1).cpu().double().clone().numpy())
    mll.zero_grad()
    return loss.item(), np.concatenate(grad)
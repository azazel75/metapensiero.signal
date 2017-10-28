# -*- coding: utf-8 -*-
# :Project:   metapensiero.signal -- utilities
# :Created:   sab 21 ott 2017 12:25:42 CEST
# :Author:    Alberto Berti <alberto@metapensiero.it>
# :License:   GNU General Public License version 3 or later
# :Copyright: Copyright © 2015, 2016, 2017 Alberto Berti
#

from collections.abc import Awaitable
import asyncio
import enum
import inspect
import logging
import weakref


logger = logging.getLogger(__name__)


class Executor:
    """A configurable executor of callable endpoints.

    :param owner: an object to reference as owner, or producer
    :param endpoints: an iterable containing the handlers to execute
    :keyword bool concurrent: optional flag indicating if the *asynchronous*
      handlers have to be executed concurrently or sequentially (the default)
    :keyword loop: optional loop
    :keyword exec_wrapper: an optional callable to call as a wrapper
    """

    def __init__(self, owner, endpoints, *, concurrent=False, loop=None,
                 exec_wrapper=None, adapt_params=True):
        self.owner = owner
        self.endpoints = list(endpoints)
        self.concurrent = concurrent
        self.loop = loop
        self.exec_wrapper = exec_wrapper
        self.adapt_params = adapt_params

    def _adapt_call_params(self, func, args, kwargs):
        signature = inspect.signature(func, follow_wrapped=False)
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD
                        for n, p in signature.parameters.items())
        if has_varkw:
            bind = signature.bind_partial(*args, **kwargs)
        else:
            bind = signature.bind_partial(*args,
                                          **{k: v for k, v in kwargs.items()
                                             if k in signature.parameters})
            bind.apply_defaults()
        return bind

    def exec_all_endpoints(self, *args, **kwargs):
        results = []
        for handler in self.endpoints:
            if isinstance(handler, weakref.ref):
                handler = handler()
            if self.adapt_params:
                bind = self._adapt_call_params(handler, args, kwargs)
                res = handler(*bind.args, **bind.kwargs)
            else:
                res = handler(*args, **kwargs)
            if isinstance(res, MultipleResults):
                results += res.results
            elif res is not NoResult:
                results.append(res)
        return MultipleResults(results, concurrent=self.concurrent, owner=self)

    def run(self, *args, **kwargs):
        """Call all the registered handlers with the arguments passed.
        If this signal is a class member, call also the handlers registered
        at class-definition time. If an external publish function is
        supplied, call it with the provided arguments.

        :returns: an instance of `~.utils.MultipleResults`
        """
        try:
            if self.exec_wrapper is None:
                return self.exec_all_endpoints(*args, **kwargs)
            else:
                # if a exec wrapper is defined, defer notification to it,
                # a callback to execute the default notification process
                result = self.exec_wrapper(self.endpoints,
                                             self.exec_all_endpoints,
                                             *args, **kwargs)
                if inspect.isawaitable(result):
                    result = pull_result(result)
                return result
        except:
            if __debug__:
                logger.exception('Error while executing')
            else:
                logger.error('Error while executing')
            raise

    __call__ = run


class MultipleResults(Awaitable):
    """An utility class containing multiple results, either *synchronous* or
    *asynchronous*. It accepts an iterable as main parameter that can contain
    actual values or *awaitables*. If any of the latter is present, it will be
    needed to ``await`` on the instance to obtain the complete set of values.

    When that is done the final results are available on the ``results``
    member and are also returned by the ``await`` expression.

    It is possible to choose how to evaluate the *awaitables*, either
    concurrently or sequentially.

    :param iterable: the incoming iterable containing the results
    :keyword concurrent: a flag indicating if the evaluation of the
      *awaitables* has to be done concurrently or sequentially
    :keyword owner: the optional creator instance
    """

    results = None
    """Contains the final results."""
    done = False
    """It's ``True`` when the results are ready for consumption."""
    has_async = False
    """``True`` if the original result set contained awaitables."""
    concurrent = False
    """``True`` if the evaluation of the awaitables is done concurrently using
    `asyncio.gather`, it's done sequentially by default."""
    owner = None
    """The optional creator of the instance passed in as a parameter, usually
    the `~.atom.Notifier` that created it."""

    def __init__(self, iterable=None, *, concurrent=False, owner=None):
        if owner is not None:
            self.owner = owner
        self.concurrent = concurrent
        self._results = list(iterable)
        self._coro_ixs = tuple(ix for ix, e in enumerate(self._results)
                               if inspect.isawaitable(e))
        if self._coro_ixs:
            self.has_async = True
        else:
            self.results = tuple(self._results)
            self.done = True
            self.has_async = False

    def __await__(self):
        task = self._completion_task(
            map(self._results.__getitem__, self._coro_ixs),
            concurrent=self.concurrent)
        return task.__await__()

    async def _completion_task(self, coro_iter=None, concurrent=False):
        if not self.done and coro_iter is not None:
            if concurrent:
                results = await asyncio.gather(*coro_iter)
                for ix, res in zip(self._coro_ixs, results):
                    self._results[ix] = res
            else:
                for ix, coro in zip(self._coro_ixs, coro_iter):
                    res = await coro
                    self._results[ix] = res
        self.results = tuple(self._results)
        del self._results
        self.done = True
        return self.results


class TokenClass:
    """A token class whose instances always generate a ``False`` bool."""

    def __bool__(self):
        return False


NoResult = TokenClass()
"""A value that is returned by a callable when there's no return value and
when ``None`` can be considered a value."""


async def pull_result(result):
    """`An utility coroutine generator to `await`` on an awaitable until the
    result is not an awaitable anymore, and return that.

    :param result: an awaitable
    :returns: a value that isn't an awaitable
    """
    while inspect.isawaitable(result):
        result = await result
    return result


class SignalError(Exception):
    """Generic error raised during signal operations"""


class HANDLERS_SORT_MODE(enum.Enum):
    """Stores the types of sort order available when retrieving class-based
    handlers. This is meaningful when using `~.atom.Signal` in together with
    classes that use `~.user.SignalAndHandlerInitMeta` as their
    ``metaclass``.
    """

    BOTTOMUP = 1
    """The class level handlers are sort from the "oldest" to the
    "newest". Handlers defined in the ancestor classes will be executed before
    of those on child classes."""
    TOPDOWN = 2
    """The class level handlers are sort from the "newest" to the
    "oldest". Handlers defined in the child classes will be executed before
    of those on ancestor classes."""

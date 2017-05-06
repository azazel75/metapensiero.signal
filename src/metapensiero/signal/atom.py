# -*- coding: utf-8 -*-
# :Project:   metapensiero.signal -- signal class
# :Created:   mer 16 dic 2015 12:28:23 CET
# :Author:    Alberto Berti <alberto@metapensiero.it>
# :License:   GNU General Public License version 3 or later
# :Copyright: Copyright (C) 2015 Alberto Berti
#

import asyncio
import contextlib
from functools import partial
import logging
import inspect
import weakref

from metapensiero.asyncio import transaction

from .compat import isawaitable
from .weak import MethodAwareWeakKeyOrderedDict
from . import ExternalSignaller
from . import SignalAndHandlerInitMeta


logger = logging.getLogger(__name__)


class InstanceProxy(object):
    """A small proxy used to get instance context when signal is a
    member of a class.
    """

    def __init__(self, signal, instance):
        self.signal = signal
        self.instance = instance
        self.subscribers = self.get_subscribers()

    def clear(self):
        """Remove all the connected handlers, for this instance"""
        self.subscribers.clear()

    def connect(self, cback):
        "See signal"
        return self.signal.connect(cback,
                                   subscribers=self.subscribers,
                                   instance=self.instance)

    def disconnect(self, cback):
        "See signal"
        return self.signal.disconnect(cback,
                                      subscribers=self.subscribers,
                                      instance=self.instance)

    def get_subscribers(self):
        """Get per-instance subscribers from the signal.
        """
        data = self.signal.instance_subscribers
        if self.instance not in data:
            data[self.instance] = MethodAwareWeakKeyOrderedDict()
        return data[self.instance]

    @property
    def loop(self):
        return getattr(self.instance, 'loop', None)

    def notify(self, *args, **kwargs):
        "See signal"
        loop = kwargs.pop('loop', self.loop)
        return self.signal.notify(*args,
                                  _subscribers=self.subscribers,
                                  _instance=self.instance,
                                  _loop=loop,
                                  **kwargs)

    def notify_no_ext(self, *args, **kwargs):
        "Like notify but avoid notifying external managers"
        loop = kwargs.pop('loop', self.loop)
        return self.signal.notify(*args,
                                  _subscribers=self.subscribers,
                                  _instance=self.instance,
                                  _loop=loop,
                                  _notify_external=False,
                                  **kwargs)

    def __repr__(self):
        return f'<Signal "{self.signal.name}" on {self.instance!r}>'


class Signal(object):
    """ The atom of event handling
    """
    _external_signaller = None
    _name = None
    _sequential_async_handlers = False

    def __init__(self, fnotify=None, fconnect=None, fdisconnect=None, name=None,
                 loop=None, external=None, sequential_async_handlers=False):
        self.name = name
        self.subscribers = MethodAwareWeakKeyOrderedDict()
        self.loop = loop or asyncio.get_event_loop()
        self.instance_subscribers = weakref.WeakKeyDictionary()
        self.external_signaller = external
        self._fnotify = fnotify
        self._fconnect = fconnect
        self._fdisconnect = fdisconnect
        self._iproxies = weakref.WeakKeyDictionary()
        self._sequential_async_handlers = sequential_async_handlers

    def __get__(self, instance, cls=None):
        if instance is not None:
            if instance not in self._iproxies:
                self._iproxies[instance] = InstanceProxy(self, instance)
            result = self._iproxies[instance]
        else:
            result = self
        return result

    def _add_to_trans(self, *items, loop=None):
        loop = loop or self.loop
        if transaction is not None:
            trans = transaction.get(None, loop=loop)
        else:
            trans = None
        if trans is None:
            res = items
        else:
            res = trans.add(*items)
        return res

    def _connect(self, subscribers, cback):
        subscribers[cback] = True

    def _create_async_results(self, sync_results, async_results, loop):
        """Create a future that will be fulfilled when all the results, both
        sync and async are computed.

        If no async results need to be computed, the future fulfills immediately.
        """
        if async_results:
            if self._sequential_async_handlers:
                res = asyncio.ensure_future(
                    self._sequential_handlers_exec(sync_results, async_results),
                    loop=loop)
            else:
                res = asyncio.Future(loop=loop)
                gathering = asyncio.gather(*async_results, loop=loop)
                gathering.add_done_callback(
                    partial(self._parallel_handlers_exec_cb, sync_results, res))
        else:
            res = asyncio.Future(loop=loop)
            res.set_result(sync_results)
        return res

    def _disconnect(self, subscribers, cback):
        if cback in subscribers:
            del subscribers[cback]

    @contextlib.contextmanager
    def _in_transaction(self, task, loop=None):
        loop = loop or self.loop
        trans = transaction.get(None, loop=self.loop, task=task)
        if trans is not None:
            trans.__enter__()
        yield trans
        if trans is not None:
            trans.__exit__(None, None, None)

    def _loop_from_instance(self, instance):
        if instance is None:
            loop = self.loop
        else:
            loop = self.__get__(instance).loop
        return loop

    def _notify(self, subscribers, instance, loop, args, kwargs,
                notify_external=True):
        """Call all the registered handlers with the arguments passed.
        If this signal is a class member, call also the handlers registered
        at class-definition time. If an external publish function is
        supplied, call it with the provided arguments at the end.

        Returns a future that will return a list of the results from the
        handlers execution.
        """
        run_async = kwargs.pop('run_async', False)
        if run_async and loop is not None:
            res =  self._notify_async(subscribers, instance, loop, args, kwargs,
                                      notify_external)
        else:
            res = self._notify_sync(subscribers, instance, loop, args, kwargs,
                                    notify_external)
        return res

    def _notify_async(self, subscribers, instance, loop, args, kwargs,
                notify_external=True):
        """See _notify"""
        fut = loop.create_future()
        def _notify_soon():
            with self._in_transaction(fut, loop):
                fut.set_result(self._notify_sync(subscribers, instance, loop,
                                                 args, kwargs, notify_external))
        loop.call_soon(_notify_soon)
        return fut

    def _notify_sync(self, subscribers, instance, loop, args, kwargs,
                notify_external=True):
        """See _notify"""
        coros = []
        results = []
        for method in subscribers:
            try:
                signature = inspect.signature(method, follow_wrapped=False)
                has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD
                                for n, p in signature.parameters.items())
                if has_varkw:
                    bind = signature.bind_partial(*args, **kwargs)
                else:
                    bind = signature.bind_partial(*args,
                        **{k:v for k, v in kwargs.items() if k in
                           signature.parameters})
                bind.apply_defaults()
                res = method(*bind.args, **bind.kwargs)
                if isawaitable(res):
                    coros.append(res)
                else:
                    results.append(res)
            except:
                logger.exception('Error in notify')
                raise
        loop = loop or self.loop
        # maybe do a round of external publishing
        if notify_external and self.external_signaller is not None:
            ext_res = self.ext_publish(instance, loop, args, kwargs)
            if isawaitable(ext_res):
                coros.append(ext_res)
        # the results are converted into a future that fulfills when all
        # the results are computed
        if not self._sequential_async_handlers:
            coros = self._add_to_trans(*coros, loop=loop)
        results = self._create_async_results(results, coros, loop)
        results.add_done_callback(partial(self._print_error_cback, instance))
        return results

    def _notify_one(self, instance, cback, *args, **kwargs):
        loop = self._loop_from_instance(instance)
        return self._notify(set((cback,)), instance, loop, args, kwargs)

    def _parallel_handlers_exec_cb(self, sync_results, result_fut,  future):
        try:
            sync_results.extend(future.result())
            result_fut.set_result(sync_results)
        except Exception as e:
            result_fut.set_exception(e)

    def _print_error_cback(self, instance, future):
        if future.exception():
            try:
                future.result()
            except Exception as e:
                logger.exception("Error occurred while running event "
                                 "callbacks for '%s' on %r", self.name, instance)

    async def _sequential_handlers_exec(self, sync_results, async_results):
        for coro in async_results:
            sync_results.append(await coro)
        return sync_results

    def connect(self, cback, subscribers=None, instance=None):
        """Add  a function or a method as an handler of this signal.
        Any handler added can be a coroutine.
        """
        if subscribers is None:
            subscribers = self.subscribers
        # wrapper
        if self._fconnect is not None:
            def _connect(cback):
                self._connect(subscribers, cback)

            _connect.notify = partial(self._notify_one, instance)
            if instance is not None:
                result = self._fconnect(instance, cback, subscribers, _connect)
            else:
                result = self._fconnect(cback, subscribers, _connect)
            if isawaitable(result):
                result = self._add_to_trans(result,
                                            loop=self._loop_from_instance(instance))[0]
        else:
            self._connect(subscribers, cback)
            result = None
        return result

    def clear(self):
        """Remove all the connected handlers"""
        self.subscribers.clear()

    def disconnect(self, cback, subscribers=None, instance=None):
        """Remove a previously added function or method from the set of the
        signal's handlers.
        """
        if subscribers is None:
            subscribers = self.subscribers
        # wrapper
        if self._fdisconnect is not None:
            def _disconnect(cback):
                self._disconnect(subscribers, cback)

            _disconnect.notify = partial(self._notify_one, instance)
            if instance is not None:
                result = self._fdisconnect(instance, cback, subscribers,
                                           _disconnect)
            else:
                result = self._fdisconnect(cback, subscribers, _disconnect)
            if isawaitable(result):
                result = self._add_to_trans(result,
                                            loop=self._loop_from_instance(instance))[0]
        else:
            self._disconnect(subscribers, cback)
            result = None
        return result

    def ext_publish(self, instance, loop, args, kwargs):
        """If 'external_signaller' is defined, calls it's publish method to
        notify external event systems.
        """
        if self.external_signaller is not None:
            # Assumes that the loop is managed by the external handler
            return self.external_signaller.publish_signal(self, instance, loop,
                                                          args, kwargs)

    @property
    def external_signaller(self):
        return self._external_signaller

    @external_signaller.setter
    def external_signaller(self, value):
        if value is not None:
            assert isinstance(value, ExternalSignaller)
        self._external_signaller = value
        if self._name and value:
            value.register_signal(self, self._name)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        self._name = value
        if value is not None and self._external_signaller:
            self._external_signaller.register_signal(self, value)

    def notify(self, *args, **kwargs):
        """Call all the registered handlers with the arguments passed. If a
        notify wrapper is defined it is called with a notify callback
        to really start the notification and a set of the registered
        class-defined and per-instance subscribers.
        """
        subscribers = kwargs.pop('_subscribers', None)
        instance = kwargs.pop('_instance', None)
        loop = kwargs.pop('_loop', None)
        notify_external = kwargs.pop('_notify_external', True)
        # merge callbacks added to the class level with those added to the
        # instance, giving the formers precedence while preserving overall order
        self_subscribers = self.subscribers.copy()
        # add in callbacks declared in the main class body and marked with
        # @handler
        if instance is not None and self.name and isinstance(instance.__class__,
                                                    SignalAndHandlerInitMeta):
            class_handlers = type(instance)._get_class_handlers(self.name, instance)
            for ch in class_handlers:
                # eventual methods are ephemeral and normally the following
                # condition would always be True for methods but the dict used
                # has logic to take that into account
                if ch not in self_subscribers:
                    self_subscribers[ch] = True
        # add in the other instance level callbacks added at runtime
        if subscribers is not None:
            for el in subscribers.keys():
                # eventual methods are ephemeral and normally the following
                # condition would always be True for methods but the dict used
                # has logic to take that into account
                if el not in self_subscribers:
                    self_subscribers[el] = True
        # finally get a list of the keys, that's all we need
        subscribers = list(self_subscribers.keys())
        if self._fnotify is not None:
            # if a notify wrapper is defined, defer notification to it,
            # a callback to execute the default notification process
            def cback(*args, **kwargs):
                return self._notify(subscribers, instance, loop, args, kwargs)
            if instance is not None:
                result = self._fnotify(instance, subscribers, cback, *args,
                                       **kwargs)
            else:
                result = self._fnotify(subscribers, cback, *args, **kwargs)
            if isawaitable(result):
                result = self._add_to_trans(result,
                                            loop=self._loop_from_instance(instance))[0]
        else:
            result = self._notify(subscribers, instance, loop, args, kwargs,
                                  notify_external=notify_external)
        return result

    def on_connect(self, fconnect):
        "On connect optional wrapper decorator"
        self._fconnect = fconnect
        return self

    def on_disconnect(self, fdisconnect):
        "On disconnect optional wrapper decorator"
        self._fdisconnect = fdisconnect
        return self

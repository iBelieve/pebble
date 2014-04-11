# This file is part of Pebble.

# Pebble is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License
# as published by the Free Software Foundation,
# either version 3 of the License, or (at your option) any later version.

# Pebble is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with Pebble.  If not, see <http://www.gnu.org/licenses/>.


from time import time, sleep
from traceback import format_exc, print_exc
from itertools import count
from threading import Thread
from collections import Callable

from .pebble import Task, TimeoutError, PoolContext


STOPPED = 0
RUNNING = 1
CLOSING = 2
CREATED = 3


class ThreadWorker(Thread):
    def __init__(self, queue, limit, expired, initializer, initargs):
        Thread.__init__(self)
        self.queue = queue
        self.limit = limit
        self.expired = expired
        self.initializer = initializer
        self.initargs = initargs
        self.daemon = True

    def run(self):
        error = None
        results = None
        counter = count()

        if self.initializer is not None:
            try:
                self.initializer(*self.initargs)
            except Exception as err:
                error = err
                error.traceback = format_exc()

        while self.limit == 0 or next(counter) < self.limit:
            task = self.queue.get()
            if task is None:  # worker terminated
                self.queue.task_done()
                return
            function = task._function
            args = task._args
            kwargs = task._kwargs
            try:
                if not task._cancelled:
                    task._timestamp = time()
                    results = function(*args, **kwargs)
            except Exception as err:
                error = err
                error.traceback = format_exc()
            finally:
                task._set(error is not None and error or results)
                if task._callback is not None:
                    try:
                        task._callback(task)
                    except:
                        print_exc()
                self.queue.task_done()
                error = None
                results = None

        ##TODO: deinitializer
        if self.expired is not None:
            self.expired.set()


class ThreadPool(object):
    def __init__(self, workers=1, task_limit=0, queue=None, queueargs=None,
                 initializer=None, initargs=None):
        self._context = PoolContext(CREATED, workers, task_limit,
                                    queue, queueargs, initializer, initargs)
        self._pool_maintainer = Thread(target=self._maintain_pool,
                                       args=[0.8])
        self._pool_maintainer.daemon = True
        self.initializer = initializer
        self.initargs = initargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        self.join()

    def _maintain_pool(self, timeout):
        while self._context.state != STOPPED:
            expired = [w for w in self._context.pool if not w.is_alive()]
            self._context.pool = [w for w in self._context.pool
                                  if w not in expired]
            for _ in range(self._context.workers - len(self._context.pool)):
                w = ThreadWorker(self._context.queue, self._context.limit,
                                 self._context.workers_event,
                                 self.initializer, self.initargs)
                w.start()
                self._context.pool.append(w)

            self._context.workers_event.wait(timeout)
            self._context.workers_event.clear()

    @property
    def initializer(self):
        return self._context.initializer

    @initializer.setter
    def initializer(self, value):
        self._context.initializer = value

    @property
    def initargs(self):
        return self._context.initargs

    @initargs.setter
    def initargs(self, value):
        self._context.initargs = value

    @property
    def active(self):
        return self._context.state == RUNNING and True or False

    def stop(self):
        """Stops the pool without performing any pending task."""
        self._context.state = STOPPED
        for w in self._context.pool:
            w.limit = - 1
        for w in self._context.pool:
            self._context.queue.put(None)

    def close(self):
        """Close the pool allowing all queued tasks to be performed."""
        self._context.state = CLOSING
        self._context.queue.join()
        self._context.state = STOPPED
        for w in self._context.pool:
            w.limit = - 1
        for w in self._context.pool:
            self._context.queue.put(None)

    def join(self, timeout=0):
        """Joins the pool waiting until all workers exited.

        If *timeout* is greater than 0,
        it block until all workers exited or raise TimeoutError.

        """
        counter = 0

        if self._context.state == RUNNING:
            raise RuntimeError('The Pool is still running')
        # if timeout is set join workers until its value
        while counter < timeout and self._context.pool:
            counter += (len(self._context.pool) + 1) / 10.0
            if self._pool_maintainer.is_alive():
                self._pool_maintainer.join(0.1)
            expired = [w for w in self._context.pool if w.join(0.1) is None
                       and not w.is_alive()]
            self._context.pool = [w for w in self._context.pool
                                  if w not in expired]
        # verify timeout expired
        if timeout > 0 and self._context.pool:
            raise TimeoutError('Workers are still running')
        # timeout not set
        self._context.pool = [w for w in self._context.pool if w.join() is None
                              and w.is_alive()]

    def schedule(self, function, args=(), identifier=None,
                 kwargs={}, callback=None):
        """Schedules *function* into the Pool, passing *args* and *kwargs*
        respectively as arguments and keyword arguments.

        If *callback* is a callable it will be executed once the function
        execution has completed with the returned *Task* as a parameter.

        A *Task* object is returned.

        """
        if self._context.state == CREATED:
            self._pool_maintainer.start()
            self._context.state = RUNNING
        elif self._context.state != RUNNING:
            raise RuntimeError('The Pool is not running')
        if not isinstance(function, Callable):
            raise ValueError('function must be callable')
        task = Task(self._context.counter, function, args, kwargs,
                    callback, 0, identifier)
        self._context.queue.put(task)

        return task

"""
Asynchronous Shared-Memory Scheduler for Dask Graphs.

This scheduler coordinates several workers to execute tasks in a dask graph in
parallel.  It depends on an apply_async function as would be found in thread or
process Pools and a corresponding Queue for worker-to-scheduler communication.

It tries to execute tasks in an order which maintains a small memory footprint
throughout execution.  It does this by running tasks that allow us to release
data resources.


Task Selection Policy
=====================

When we complete a task we add more data in to our set of available data; this
new data makes new tasks available.  We preferentially choose tasks that were
just made available in a last-in-first-out fashion.  We implement this as a
simple stack.  This results in more depth-first rather than breadth first
behavior which encourages us to process batches of data to completion before
starting in on new data when possible.

When the addition of new data readies multiple tasks simultaneously we add
tasks to the stack in sorted order so that tasks with greater keynames are run
first.  This can be handy to break ties in a predictable fashion.


State
=====

Many functions pass around a ``state`` variable that holds the current state of
the computation.  This variable consists of several other dictionaries and
sets, explained below.

Constant state
--------------

1.  dependencies: {x: [a, b ,c]} a,b,c, must be run before x
2.  dependents: {a: [x, y]} a must run before x or y

Changing state
--------------

### Data

1.  cache: available concrete data.  {key: actual-data}
2.  released: data that we've seen, used, and released because it is no longer
    needed

### Jobs

1.  ready: A fifo stack of ready-to-run tasks
2.  ready-set: A set of the data above for rapid access
3.  running: A set of tasks currently in execution
4.  finished: A set of finished tasks
5.  waiting: which tasks are still waiting on others :: {key: {keys}}
    Real-time equivalent of dependencies
6.  waiting_data: available data to yet-to-be-run-tasks :: {key: {keys}}
    Real-time equivalent of dependents


Example
-------

>>> import pprint
>>> dsk = {'x': 1, 'y': 2, 'z': (inc, 'x'), 'w': (add, 'z', 'y')}
>>> pprint.pprint(start_state_from_dask(dsk)) # doctest: +NORMALIZE_WHITESPACE
{'cache': {'x': 1, 'y': 2},
 'dependencies': {'w': set(['y', 'z']),
                  'x': set([]),
                  'y': set([]),
                  'z': set(['x'])},
 'dependents': {'w': set([]),
                'x': set(['z']),
                'y': set(['w']),
                'z': set(['w'])},
 'finished': set([]),
 'ready': ['z'],
 'ready-set': set(['z']),
 'released': set([]),
 'running': set([]),
 'waiting': {'w': set(['z'])},
 'waiting_data': {'x': set(['z']),
                  'y': set(['w']),
                  'z': set(['w'])}}

Optimizations
=============

We build this scheduler with out-of-core array operations in mind.  To this end
we have encoded some particular optimizations.

Compute to release data
-----------------------

When we choose a new task to execute we often have many options.  Policies at
this stage are cheap and can significantly impact performance.  One could
imagine policies that expose parallelism, drive towards a particular output,
etc..

Our current policy is to run tasks that were most recently made available.


Inlining computations
---------------------

We hold on to intermediate computations either in memory or on disk.

For very cheap computations that may emit new copies of the data, like
``np.transpose`` or possibly even ``x + 1`` we choose not to store these as
separate pieces of data / tasks.  Instead we combine them with the computations
that require them.  This may result in repeated computation but saves
significantly on space and computation complexity.

See the function ``inline_functions`` for more information.
"""
from __future__ import absolute_import, division, print_function

import sys
import traceback
from operator import add
from .core import istask, flatten, reverse_dict, get_dependencies, ishashable
from .context import _globals
from .order import order

def inc(x):
    return x + 1

def double(x):
    return x * 2


DEBUG = False


def start_state_from_dask(dsk, cache=None, sortkey=None):
    """ Start state from a dask

    Example
    -------

    >>> dsk = {'x': 1, 'y': 2, 'z': (inc, 'x'), 'w': (add, 'z', 'y')}
    >>> import pprint
    >>> pprint.pprint(start_state_from_dask(dsk)) # doctest: +NORMALIZE_WHITESPACE
    {'cache': {'x': 1, 'y': 2},
     'dependencies': {'w': set(['y', 'z']),
                      'x': set([]),
                      'y': set([]),
                      'z': set(['x'])},
     'dependents': {'w': set([]),
                    'x': set(['z']),
                    'y': set(['w']),
                    'z': set(['w'])},
     'finished': set([]),
     'ready': ['z'],
     'ready-set': set(['z']),
     'released': set([]),
     'running': set([]),
     'waiting': {'w': set(['z'])},
     'waiting_data': {'x': set(['z']),
                      'y': set(['w']),
                      'z': set(['w'])}}
    """
    if sortkey is None:
        sortkey = order(dsk).get
    if cache is None:
        cache = _globals['cache']
    if cache is None:
        cache = dict()
    data_keys = set()
    for k, v in dsk.items():
        if not istask(v) and (not ishashable(v) or v not in dsk):
            cache[k] = v
            data_keys.add(k)

    dependencies = dict((k, get_dependencies(dsk, k)) for k in dsk)
    waiting = dict((k, v.copy()) for k, v in dependencies.items()
                                 if k not in data_keys)

    dependents = reverse_dict(dependencies)
    for a in data_keys:
        for b in dependents[a]:
            waiting[b].remove(a)
    waiting_data = dict((k, v.copy()) for k, v in dependents.items() if v)

    ready_set = set([k for k, v in waiting.items() if not v])
    ready = sorted(ready_set, key=sortkey)
    waiting = dict((k, v) for k, v in waiting.items() if v)

    state = {'dependencies': dependencies,
             'dependents': dependents,
             'waiting': waiting,
             'waiting_data': waiting_data,
             'cache': cache,
             'ready': ready,
             'ready-set': ready_set,
             'running': set(),
             'finished': set(),
             'released': set()}

    return state


'''
Running tasks
-------------

When we execute tasks we both

1.  Perform the actual work of collecting the appropriate data and calling the function
2.  Manage administrative state to coordinate with the scheduler
'''

def _execute_task(arg, cache, dsk=None):
    """ Do the actual work of collecting data and executing a function

    Examples
    --------

    >>> cache = {'x': 1, 'y': 2}

    Compute tasks against a cache
    >>> _execute_task((add, 'x', 1), cache)  # Compute task in naive manner
    2
    >>> _execute_task((add, (inc, 'x'), 1), cache)  # Support nested computation
    3

    Also grab data from cache
    >>> _execute_task('x', cache)
    1

    Support nested lists
    >>> list(_execute_task(['x', 'y'], cache))
    [1, 2]

    >>> list(map(list, _execute_task([['x', 'y'], ['y', 'x']], cache)))
    [[1, 2], [2, 1]]

    >>> _execute_task('foo', cache)  # Passes through on non-keys
    'foo'
    """
    if isinstance(arg, list):
        return (_execute_task(a, cache) for a in arg)
    elif istask(arg):
        func, args = arg[0], arg[1:]
        args2 = [_execute_task(a, cache) for a in args]
        return func(*args2)
    elif not ishashable(arg):
        return arg
    elif arg in cache:
        return cache[arg]
    else:
        return arg


def execute_task(key, task, data, queue, get_id, raise_on_exception=False):
    """
    Compute task and handle all administration

    See also:
        _execute_task - actually execute task
    """
    try:
        result = _execute_task(task, data)
        id = get_id()
        result = key, result, None, id
    except Exception as e:
        if raise_on_exception:
            raise
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = ''.join(traceback.format_tb(exc_traceback))
        result = key, e, tb, None
    try:
        queue.put(result)
    except Exception as e:
        if raise_on_exception:
            raise
        exc_type, exc_value, exc_traceback = sys.exc_info()
        tb = ''.join(traceback.format_tb(exc_traceback))
        queue.put((key, e, tb, None))


def release_data(key, state, delete=True):
    """ Remove data from temporary storage

    See Also
        finish_task
    """
    if key in state['waiting_data']:
        assert not state['waiting_data'][key]
        del state['waiting_data'][key]

    state['released'].add(key)

    if delete:
        del state['cache'][key]


def finish_task(dsk, key, state, results, sortkey, delete=True,
                release_data=release_data):
    """
    Update execution state after a task finishes

    Mutates.  This should run atomically (with a lock).
    """
    if key in state['ready-set']:
        state['ready-set'].remove(key)

    for dep in sorted(state['dependents'][key], key=sortkey):
        s = state['waiting'][dep]
        s.remove(key)
        if not s:
            del state['waiting'][dep]
            state['ready-set'].add(dep)
            state['ready'].append(dep)

    for dep in state['dependencies'][key]:
        if dep in state['waiting_data']:
            s = state['waiting_data'][dep]
            s.remove(key)
            if not s and dep not in results:
                if DEBUG:
                    from chest.core import nbytes
                    print("Key: %s\tDep: %s\t NBytes: %.2f\t Release" % (key, dep,
                        sum(map(nbytes, state['cache'].values()) / 1e6)))
                release_data(dep, state, delete=delete)
        elif delete and dep not in results:
            release_data(dep, state, delete=delete)

    state['finished'].add(key)
    state['running'].remove(key)

    return state


def nested_get(ind, coll, lazy=False):
    """ Get nested index from collection

    Examples
    --------

    >>> nested_get(1, 'abc')
    'b'
    >>> nested_get([1, 0], 'abc')
    ('b', 'a')
    >>> nested_get([[1, 0], [0, 1]], 'abc')
    (('b', 'a'), ('a', 'b'))
    """
    if isinstance(ind, list):
        if lazy:
            return (nested_get(i, coll, lazy=lazy) for i in ind)
        else:
            return tuple([nested_get(i, coll, lazy=lazy) for i in ind])
    else:
        return coll[ind]


def default_get_id():
    """Default get_id"""
    return None


'''
Task Selection
--------------

We often have a choice among many tasks to run next.  This choice is both
cheap and can significantly impact performance.

We currently select tasks that have recently been made ready.  We hope that
this first-in-first-out policy reduces memory footprint
'''

'''
`get`
-----

The main function of the scheduler.  Get is the main entry point.
'''

def get_async(apply_async, num_workers, dsk, result, cache=None,
              queue=None, get_id=default_get_id, raise_on_exception=False,
              start_callback=None, end_callback=None,
              rerun_exceptions_locally=None,
              **kwargs):
    """ Asynchronous get function

    This is a general version of various asynchronous schedulers for dask.  It
    takes a an apply_async function as found on Pool objects to form a more
    specific ``get`` method that walks through the dask array with parallel
    workers, avoiding repeat computation and minimizing memory use.

    This function evaluates the entire graph, regardless of the given output
    keys.  You may want to cull your graph ahead of time with
    ``dask.optimize.cull``.

    Parameters
    ----------

    apply_async : function
        Asynchronous apply function as found on Pool or ThreadPool
    num_workers : int
        The number of active tasks we should have at any one time
    dsk: dict
        A dask dictionary specifying a workflow
    result : key or list of keys
        Keys corresponding to desired data
    cache : dict-like, optional
        Temporary storage of results
    get_id : callable, optional
        Function to return the worker id, takes no arguments. Examples are
        `threading.current_thread` and `multiprocessing.current_process`.
    rerun_exceptions_locally : bool, optional
        Whether to rerun failing tasks in local process to enable debugging
        (False by default)
    start_callback : function, optional
        Callback run every time a new task is started. Receives the key of the
        task to be run, the dask, and the scheduler state. At the end of
        computation this will called a final time with ``None`` as the key, as
        no new tasks were added at that tick.
    end_callback : function, optional
        Callback run every time a task is finished. Receives the key of the
        task just run, resulting value, dask, scheduler state, and id of the
        worker that ran the task.  At the end of computation this will called a
        final time with ``None`` for key, value and worker id, as no new tasks
        were finished at that step.

    See Also
    --------

    threaded.get
    """
    assert queue

    if isinstance(result, list):
        result_flat = set(flatten(result))
    else:
        result_flat = set([result])
    results = set(result_flat)

    keyorder = order(dsk)

    state = start_state_from_dask(dsk, cache=cache, sortkey=keyorder.get)

    if rerun_exceptions_locally is None:
        rerun_exceptions_locally = _globals.get('rerun_exceptions_locally', False)

    if state['waiting'] and not state['ready']:
        raise ValueError("Found no accessible jobs in dask")

    def fire_task():
        """ Fire off a task to the thread pool """
        # Choose a good task to compute
        key = state['ready'].pop()
        state['ready-set'].remove(key)
        state['running'].add(key)
        if start_callback:
            start_callback(key, dsk, state)

        # Prep data to send
        data = dict((dep, state['cache'][dep])
                    for dep in get_dependencies(dsk, key))
        # Submit
        apply_async(execute_task, args=[key, dsk[key], data, queue,
                                        get_id, raise_on_exception])

    # Seed initial tasks into the thread pool
    while state['ready'] and len(state['running']) < num_workers:
        fire_task()

    # Main loop, wait on tasks to finish, insert new ones
    while state['waiting'] or state['ready'] or state['running']:
        key, res, tb, worker_id = queue.get()
        if isinstance(res, Exception):
            if rerun_exceptions_locally:
                data = dict((dep, state['cache'][dep])
                            for dep in get_dependencies(dsk, key))
                task = dsk[key]
                _execute_task(task, data)  # Re-execute locally
            else:
                raise type(res)(
                "Exception occurred in remote worker.\n\n"
                "Something you've asked dask to compute raised an exception.\n"
                "That exception and the traceback are copied below.\n"
                "To use pdb, rerun the computation with the keyword argument\n"
                "    dask.set_options(rerun_exceptions_locally=True)\n"
                "    or\n"
                "    dataset.compute(rerun_exceptions_locally=True)\n\n"
                "The original exception and traceback follow below:\n\n"
                    + str(res) + "\n\nTraceback:\n" + tb)
        state['cache'][key] = res
        finish_task(dsk, key, state, results, keyorder.get)
        if end_callback:
            end_callback(key, res, dsk, state, worker_id)
        while state['ready'] and len(state['running']) < num_workers:
            fire_task()

    # Final reporting
    while state['running'] or not queue.empty():
        key, res, tb, worker_id = queue.get()

    # Run the callbacks one last time with the final state
    if start_callback:
        start_callback(None, dsk, state)
    if end_callback:
        end_callback(None, None, dsk, state, None)

    return nested_get(result, state['cache'])


""" Synchronous concrete version of get_async

Usually we supply a multi-core apply_async function.  Here we provide a
sequential one.  This is useful for debugging and for code dominated by the
GIL
"""

def apply_sync(func, args=(), kwds={}):
    """ A naive synchronous version of apply_async """
    return func(*args, **kwds)


def get_sync(dsk, keys, **kwargs):
    from .compatibility import Queue
    queue = Queue()
    return get_async(apply_sync, 1, dsk, keys, queue=queue,
                     raise_on_exception=True, **kwargs)


'''
Debugging
---------

The threaded nature of this project presents challenging to normal unit-test
and debug workflows.  Visualization of the execution state has value.

Our main mechanism is a visualization of the execution state as colors on our
normal dot graphs (see dot module).
'''

def visualize(dsk, state, filename='dask'):
    """ Visualize state of compputation as dot graph """
    from dask.dot import dot_graph
    data, func = color_nodes(dsk, state)
    dot_graph(dsk, filename=filename, data_attributes=data,
              func_attributes=func)


def color_nodes(dsk, state):
    data, func = dict(), dict()
    for key in dsk:
        func[key] = {'color': 'gray'}
        data[key] = {'color': 'gray'}

    for key in state['released']:
        data[key] = {'color': 'blue'}

    for key in state['cache']:
        data[key] = {'color': 'red'}

    for key in state['finished']:
            func[key] = {'color': 'blue'}
    for key in state['running']:
            func[key] = {'color': 'red'}

    for key in dsk:
        func[key]['penwidth'] = 4
        data[key]['penwidth'] = 4
    return data, func


def sortkey(item):
    """ Sorting key function that is robust to different types

    Both strings and tuples are common key types in dask graphs.
    However In Python 3 one can not compare strings with tuples directly.
    This function maps many types to a form where they can be compared

    Examples
    --------
    >>> sortkey('Hello')
    ('str', 'Hello')

    >>> sortkey(('x', 1))
    ('tuple', ('x', 1))
    """
    return (type(item).__name__, item)

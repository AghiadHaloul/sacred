#!/usr/bin/env python
# coding=utf-8
from __future__ import division, print_function, unicode_literals

import signal

import hashlib
import json
import os
import platform
import shutil
import tempfile
import time

from copy import copy
from datetime import datetime, timedelta
from pkg_resources import parse_version

import pymongo
import gridfs
import docker

from sacred import Experiment
from sacred.observers.mongo import MongoDbOption
ac = Experiment('acolyte')


@ac.config
def cfg():
    run_db = "localhost:27017:INPUT"
    query = {'status': 'QUEUED'}
    copy_files = []
    apt_packages = []
    base_dir = '_acolyte'
    image_tag = 'acolyte/{name}:{_id}'
    volumes = {'/home/greff/Datasets': {'bind': '/home/greff/Datasets',
                                        'mode': 'ro'}}
    replace_requirements = {}
    waiting_interval = 5  # in seconds


@ac.capture
def run_database_setup(run_db):
    # parse options
    db_specs = MongoDbOption.parse_mongo_db_arg(run_db)
    url = db_specs['url']
    db_name = db_specs.get('db_name', 'sacred')
    collection_name = db_specs.get('collection', 'runs')
    client = pymongo.MongoClient(url)
    db = client[db_name]
    runs = db[collection_name]
    fs = gridfs.GridFS(db)
    mongo_arg = "{url}:{db}.{collection}:{overwrite}".format(
        url=url, db=db_name, collection=collection_name, overwrite='{_id}')
    return db, runs, fs, mongo_arg


def get_status_queries():
    now = datetime.utcnow()
    patience = timedelta(seconds=120)
    stats_queries = {
        'TOTAL': {},
        'RUNNING': {'status': 'RUNNING', 'heartbeat': {'$gt': now - patience}},
        'COMPLETED': {'status': 'COMPLETED'},
        'INTERRUPTED': {'status': 'INTERRUPTED'},
        'FAILED': {'status': 'FAILED'},
        'TIMEOUT': {'status': 'TIMEOUT'},
        'DIED': {'status': 'RUNNING', 'heartbeat': {'$lt': now - patience}},
        'QUEUED': {'status': 'QUEUED'}}
    return stats_queries


# ###################### DB STATUS ########################################## #

@ac.command(unobserved=True)
def db_status(run_db):
    print('Status for {}'.format(run_db))
    db, runs, fs, _ = run_database_setup()
    ignored_collections = {'fs.files', 'system.indexes', 'fs.chunks'}
    collections = sorted([coll for coll in db.collection_names()
                          if coll not in ignored_collections])
    stats_queries = get_status_queries()
    counts = [{status: db[coll].find(q).count()
               for status, q in stats_queries.items()}
              for coll in collections]
    import pandas as pd
    df = pd.DataFrame(counts, index=collections)
    column_order = ['TOTAL', 'RUNNING', 'COMPLETED', 'INTERRUPTED', 'FAILED',
                    'TIMEOUT', 'DIED', 'QUEUED']
    print('\n', df[column_order], '\n')


# ############################ RUN ########################################## #

@ac.capture
def get_next_run(runs, query, blacklist=(), _run=None):
    _run.info['status'] = 'SEARCHING'
    q = copy(query)
    q['_id'] = {'$nin': sorted(blacklist)}

    return runs.find_one_and_update(q, {'$set': {'status': 'ACQUIRED'}},
                                    return_document=pymongo.ReturnDocument.AFTER)


def _write_file(base_dir, filename, source, blocksize=2**20):
    full_name = os.path.join(base_dir, filename)
    os.makedirs(os.path.dirname(full_name), exist_ok=True)
    if isinstance(source, str) and os.path.exists(source):
        shutil.copy2(source, full_name)
    elif isinstance(source, str):
        with open(full_name, 'wt') as f:
            f.write(source)
    else:
        with open(full_name, 'wb') as f:
            buf = source.read(blocksize)
            while buf:
                f.write(buf)
                buf = source.read(blocksize)


def _get_hash(dir_or_file, sha=None, blocksize=2**20):
    sha = hashlib.sha256() if sha is None else sha

    if os.path.isfile(dir_or_file):
        # get hash of file
        with open(dir_or_file, "rb") as f:
            buf = f.read(blocksize)
            while buf:
                sha.update(buf)
                buf = f.read(blocksize)
    elif os.path.isdir(dir_or_file):
        for f in sorted(os.listdir(dir_or_file)):
            sha.update(f)  # to capture renaming of contained files
            _get_hash(os.path.join(dir_or_file, f), sha, blocksize)
    else:
        print('ignoring weird file {}'.format(dir_or_file))
    return sha


def get_re_sources(run_entry, fs):
    re_sources = {source[0]: fs.get(source[1])
                  for source in run_entry['experiment']['sources']}
    re_sources.update({resource[0]: fs.get(resource[1])
                       for resource in run_entry.get('resources', ())})
    return re_sources


def _get_truncated_python_version(run_entry):
    host_info = run_entry.get('host', {})
    version = host_info.get('python_version', platform.python_version())
    short_version = parse_version(version)._version.release[:2]
    return '{}.{}'.format(*short_version)


@ac.capture
def make_docker_dir(run_dir, requirements, re_sources, python_version,
                    copy_files=(), apt_packages=()):
    os.makedirs(run_dir, exist_ok=True)

    if isinstance(requirements, (tuple, list)):
        requirements = "\n".join(requirements)

    _write_file(run_dir, 'requirements.txt',
                requirements)

    for filename, fp in re_sources.items():
        _write_file(run_dir, filename, fp)

    for filename in copy_files:
        shutil.copy2(filename, run_dir)

    apt_command = ''
    if apt_packages:
        apt_command = '''
            RUN apt-get update && apt-get install -y \\
                {} \\
                && rm -rf /var/lib/apt/lists/*'''.format(' \\\n'.join(apt_packages))

    # Dockerfile
    dockerfile = """# Use an official Python runtime as a parent image
    FROM python:{python_version}-slim

    # Set the working directory to /sacred
    WORKDIR /sacred

    # Copy the current directory contents into the container at /app
    ADD . /sacred

    {apt_command}

    RUN pip install -U pip pymongo

    # Install any needed packages specified in requirements.txt
    RUN pip install -r requirements.txt
    """.format(python_version=python_version,
               apt_command=apt_command)
    _write_file(run_dir, 'Dockerfile', dockerfile)


@ac.capture
def make_run_dir(base_dir):
    worker_dir = os.path.abspath(base_dir)
    run_dir = os.path.join(worker_dir, 'current_run')
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    os.makedirs(run_dir, exist_ok=True)
    return worker_dir, run_dir


@ac.capture
def prepare_requirements(run, replace_requirements):
    req = {d.split('==', 1)[0]: d for d in run['experiment']['dependencies']}
    req.update(replace_requirements)
    requirements = "\n".join([r for k, r in sorted(req.items(), key=lambda x: x[0])])
    return requirements


@ac.capture
def build_image(run, fs, dclient, mongo_arg, image_tag, volumes, _log, _run):
    _run.info['status'] = 'BUILDING'
    re_sources = get_re_sources(run, fs)
    py_version = _get_truncated_python_version(run)
    requirements = prepare_requirements(run)
    worker_dir, run_dir = make_run_dir()
    _log.info('Creating run directory: %s', run_dir)
    make_docker_dir(run_dir, requirements, re_sources, py_version)

    tag = image_tag.format(name=run['experiment']['name'],
                           _id=run['_id'])
    _log.info('Building docker image %s', tag)
    dclient.images.build(path=run_dir, tag=tag)
    command = "python {mainfile} with {config} -m {target_db}".format(
        mainfile=run['experiment']['mainfile'],
        config='worker/config.json',
        target_db=mongo_arg.format(_id=run['_id'])
    )
    with open(os.path.join(worker_dir, 'config.json'), 'wt') as f:
        json.dump(run['config'], f)
    vols = copy(volumes)
    vols[worker_dir] = {'bind': '/sacred/worker', 'mode': 'ro'}
    return tag, command, vols


@ac.capture
def run_container(dclient, tag, command, vols, _run, _log):
    _run.info['status'] = 'RUNNING'
    _log.info('Executing command "%s"', command)
    start_time = time.time()
    try:
        dclient.containers.run(tag, command, volumes=vols, remove=True)
        outcome = 'SUCCESS'
    except docker.errors.ContainerError as e:
        print(e.args)
        print(e.stderr.decode())
        outcome = e.args
    except Exception as e:
        print(e)
        outcome = e.args
    elapsed_time = time.time() - start_time
    return outcome, elapsed_time


@ac.automain
def run(waiting_interval, _log, _run):
    keep_going = [True]

    def exit_gracefully(signal, frame):
        keep_going[0] = False
        _log.warning('Interrupt has been registered -- '
                     'Will stop after completing the current run.')

    signal.signal(signal.SIGINT, exit_gracefully)
    db, runs, fs, mongo_arg = run_database_setup()
    blacklist = set()
    dclient = docker.from_env()
    t = time.time()
    _run.info['history'] = []
    while keep_going[0]:
        run = get_next_run(runs, blacklist=blacklist)
        if run is None:
            time.sleep(waiting_interval)
            continue
        waited_for = time.time() - t
        _log.info('Found run (_id=%s).', run['_id'])
        _run.info['current_run'] = run['_id']

        tag, command, vols = build_image(run, fs, dclient, mongo_arg)
        outcome, elapsed_time = run_container(dclient, tag, command, vols)
        if outcome != 'SUCCESS':
            blacklist.add(run['_id'])
            _log.warning('Added {} to blacklist, which now contains {}'
                         .format(run['_id'], blacklist))

        _run.info['history'].append({
            '_id': run['_id'],
            'outcome': outcome,
            'elapsed': elapsed_time,
            'waited_for': waited_for
        })
        t = time.time()

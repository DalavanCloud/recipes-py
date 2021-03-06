#!/usr/bin/env vpython
# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import argparse
import base64
import itertools
import json
import unittest

from cStringIO import StringIO

import mock

import test_env

from recipe_engine import common_args
from recipe_engine import fetch
from recipe_engine import package_io
from recipe_engine import package_pb2
from recipe_engine.third_party import subprocess42


CPE = subprocess42.CalledProcessError
IRC = package_io.InfraRepoConfig.RELPATH


class NoMoreExpectatedCalls(ValueError):
  pass


def multi(*side_effect_funcs):
  l = len(side_effect_funcs)
  it = iter(side_effect_funcs)
  def _inner(*args, **kwargs):
    try:
      return it.next()(*args, **kwargs)
    except StopIteration:
      raise NoMoreExpectatedCalls(
        'multi() ran out of values (i=%d): f(*%r, **%r)' % (l, args, kwargs))
  return _inner


class TestGit(unittest.TestCase):

  def setUp(self):
    fetch.Backend._GIT_METADATA_CACHE = {}
    mock.patch('recipe_engine.fetch.GitBackend._GIT_BINARY', 'GIT').start()
    self.addCleanup(mock.patch.stopall)

  def assertMultiDone(self, mocked_call):
    with self.assertRaises(NoMoreExpectatedCalls):
      mocked_call()

  def g(self, args, data_or_exception=''):
    full_args = ['GIT']
    if args[0] != 'init':  # init is special
      full_args += ['-c', 'advice.detachedHead=false']
    full_args += args

    if isinstance(data_or_exception, Exception):
      def _inner(*real_args):
        self.assertListEqual(list(real_args), full_args)
        raise data_or_exception
    else:
      def _inner(*real_args):
        self.assertListEqual(list(real_args), full_args)
        return data_or_exception
    return _inner

  def g_metadata_calls(self, dirname='dir', commit='a'*40,
                       email='foo@example.com', msg='hello\nworld',
                       commit_timestamp=1492131405, config=None,
                       diff=('foo', 'bar')):
    config = config or {'api_version': 2}

    return [
      self.g([
        '-C', dirname, 'show', '-s', '--format=%aE%n%ct%n%B', commit
      ], '%s\n%d\n%s\n' % (email, commit_timestamp, msg)),
      self.g([
        '-C', dirname, 'cat-file', 'blob', commit+':'+IRC
      ], json.dumps(config)),
      self.g([
        '-C', dirname,
        'diff-tree', '-r', '--no-commit-id', '--name-only', commit+'^!',
      ], '\n'.join(diff))
    ]

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  def test_fresh_clone(self, git, isdir):
    isdir.return_value = False
    git.side_effect = multi(*([
      self.g(['init', 'dir']),
      self.g(['-C', 'dir',  'ls-remote', 'repo', 'revision'], 'a'*40),
    ] + self.g_metadata_calls() + [
      self.g(['-C', 'dir', 'diff', '--quiet', 'a'*40], CPE('', 1)),
      self.g(['-C', 'dir', 'reset', '-q', '--hard', 'a'*40])
    ]))

    fetch.GitBackend('dir', 'repo').checkout('revision')

    self.assertMultiDone(git)

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  def test_existing_checkout(self, git, isdir):
    isdir.return_value = True
    git.side_effect = multi(*([
      self.g(['-C', 'dir', 'ls-remote', 'repo', 'revision'], 'a'*40)
    ] + self.g_metadata_calls() + [
      self.g(['-C', 'dir', 'diff', '--quiet', 'a'*40], CPE('', 1)),
      self.g(['-C', 'dir', 'reset', '-q', '--hard', 'a'*40])
    ]))

    fetch.GitBackend('dir', 'repo').checkout('revision')

    self.assertMultiDone(git)
    isdir.assert_has_calls([
      mock.call('dir/.git'),
    ])

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  def test_existing_checkout_same_revision(self, git, isdir):
    isdir.return_value = True
    git.side_effect = multi(*([
      self.g(['-C', 'dir', 'ls-remote', 'repo', 'revision'], 'a'*40)
    ] + self.g_metadata_calls() + [
      self.g(['-C', 'dir', 'diff', '--quiet', 'a'*40]),
    ]))

    fetch.GitBackend('dir', 'repo').checkout('revision')

    self.assertMultiDone(git)
    isdir.assert_has_calls([
      mock.call('dir/.git'),
    ])

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  def test_unclean_filesystem(self, git, isdir):
    isdir.return_value = False
    def _mock_execute(*_args):
      raise subprocess42.CalledProcessError(1, 'bad stuff')
    git.side_effect = _mock_execute

    with self.assertRaises(fetch.GitError):
      fetch.GitBackend('dir', 'repo').checkout('revision')

    git.assert_called_once_with('GIT', 'init', 'dir')

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  def test_rev_parse_fail(self, git, isdir):
    isdir.return_value = True
    git.side_effect = multi(*(
      self.g(['-C', 'dir', 'ls-remote', 'repo', 'revision'], 'a'*40),

      self.g(
        ['-C', 'dir', 'show', '-s', '--format=%aE%n%ct%n%B', 'a'*40],
        CPE(1, 'nope')),

      self.g(['-C', 'dir', 'fetch', 'repo', 'revision']),
      self.g(['-C', 'dir', 'diff', '--quiet', 'a'*40], CPE('', 1)),
      self.g(['-C', 'dir', 'reset', '-q', '--hard', 'a'*40]),
    ))

    fetch.GitBackend('dir', 'repo').checkout('revision')

    self.assertMultiDone(git)

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  def test_commit_metadata_empty_recipes_path(self, git, isdir):
    isdir.return_value = False
    git.side_effect = multi(*([
      self.g(['init', 'dir']),
      self.g(['-C', 'dir', 'ls-remote', 'repo', 'revision'], 'a'*40),
    ] + self.g_metadata_calls()))

    result = fetch.GitBackend('dir', 'repo').commit_metadata('revision')
    self.assertEqual(result, fetch.CommitMetadata(
      revision = 'a'*40,
      author_email = 'foo@example.com',
      commit_timestamp = 1492131405,
      message_lines = ('hello', 'world'),
      spec = package_pb2.Package(api_version=2),
      roll_candidate = True,
    ))
    self.assertMultiDone(git)

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  @mock.patch('recipe_engine.fetch.gitattr_checker.AttrChecker.check_files')
  def test_commit_metadata_not_interesting(self, attr_checker, git, isdir):
    attr_checker.side_effect = [False]
    isdir.return_value = False
    spec = package_pb2.Package(api_version=2, recipes_path='recipes')
    json_spec = package_io.dump_obj(spec)

    git.side_effect = multi(*([
      self.g(['init', 'dir']),
      self.g(['-C', 'dir', 'ls-remote', 'repo', 'revision'], 'a'*40),
    ] + self.g_metadata_calls(config=json_spec)))

    result = fetch.GitBackend('dir', 'repo').commit_metadata('revision')
    self.assertEqual(result, fetch.CommitMetadata(
      revision = 'a'*40,
      author_email = 'foo@example.com',
      commit_timestamp = 1492131405,
      message_lines = ('hello', 'world'),
      spec = spec,
      roll_candidate = False,
    ))
    self.assertMultiDone(git)
    attr_checker.assert_called_with('a'*40, set(['foo', 'bar']))

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  def test_commit_metadata_IRC_change(self, git, isdir):
    isdir.return_value = False
    spec = package_pb2.Package(api_version=2, recipes_path='recipes')
    json_spec = package_io.dump_obj(spec)

    git.side_effect = multi(*([
      self.g(['init', 'dir']),
      self.g(['-C', 'dir', 'ls-remote', 'repo', 'revision'], 'a'*40),
    ] + self.g_metadata_calls(config=json_spec, diff=tuple([IRC]))))

    result = fetch.GitBackend('dir', 'repo').commit_metadata('revision')
    self.assertEqual(result, fetch.CommitMetadata(
      revision = 'a'*40,
      author_email = 'foo@example.com',
      commit_timestamp = 1492131405,
      message_lines = ('hello', 'world'),
      spec = spec,
      roll_candidate = True,
    ))
    self.assertMultiDone(git)

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  def test_commit_metadata_recipes_change(self, git, isdir):
    isdir.return_value = False
    spec = package_pb2.Package(api_version=2, recipes_path='recipes')
    json_spec = package_io.dump_obj(spec)

    git.side_effect = multi(*([
      self.g(['init', 'dir']),
      self.g(['-C', 'dir', 'ls-remote', 'repo', 'revision'], 'a'*40),
    ] + self.g_metadata_calls(config=json_spec, diff=tuple(['recipes/foo']))))

    result = fetch.GitBackend('dir', 'repo').commit_metadata('revision')
    self.assertEqual(result, fetch.CommitMetadata(
      revision = 'a'*40,
      author_email = 'foo@example.com',
      commit_timestamp = 1492131405,
      message_lines = ('hello', 'world'),
      spec = spec,
      roll_candidate = True,
    ))
    self.assertMultiDone(git)

  @mock.patch('os.path.isdir')
  @mock.patch('recipe_engine.fetch.GitBackend._execute')
  @mock.patch('recipe_engine.fetch.gitattr_checker.AttrChecker.check_files')
  def test_commit_metadata_tagged_change(self, attr_checker, git, isdir):
    attr_checker.side_effect = [True]
    isdir.return_value = False
    spec = package_pb2.Package(api_version=2, recipes_path='recipes')
    json_spec = package_io.dump_obj(spec)

    git.side_effect = multi(*([
      self.g(['init', 'dir']),
      self.g(['-C', 'dir', 'ls-remote', 'repo', 'revision'], 'a'*40),
    ] + self.g_metadata_calls(config=json_spec)))

    result = fetch.GitBackend('dir', 'repo').commit_metadata('revision')
    self.assertEqual(result, fetch.CommitMetadata(
      revision = 'a'*40,
      author_email = 'foo@example.com',
      commit_timestamp = 1492131405,
      message_lines = ('hello', 'world'),
      spec = spec,
      roll_candidate = True,
    ))
    self.assertMultiDone(git)
    attr_checker.assert_called_with('a'*40, set(['foo', 'bar']))


if __name__ == '__main__':
  unittest.main()

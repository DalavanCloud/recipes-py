# Copyright 2017 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import base64
import json

from google.protobuf import json_format
from google.protobuf import timestamp_pb2

from recipe_engine import recipe_test_api

from .proto import build_pb2
from .proto import common_pb2
from . import util


class BuildbucketTestApi(recipe_test_api.RecipeTestApi):
  # Expose protobuf messages to the users of buildbucket module.
  build_pb2 = build_pb2
  common_pb2 = common_pb2

  def build(self, build_message):
    """Emulates a buildbucket build.

    build_message is a buildbucket.build_pb2.Build.
    """
    return self.m.properties(**{
      '$recipe_engine/buildbucket': {
        'build': json.loads(json_format.MessageToJson(build_message)),
      },
    })

  def _default_git_repo(self, project):  # pragma: no cover
    if 'internal' in project:
      return 'https://chrome-internal.googlesource.com/' + project
    return 'https://chromium.googlesource.com/' + project

  def ci_build_message(
      self,
      project='project',
      bucket='ci',  # shortname.
      builder='builder',
      git_repo=None,
      git_ref='refs/heads/master',
      revision='2d72510e447ab60a9728aeea2362d8be2cbd7789',
      build_number=0):
    """Returns a typical buildbucket CI build scheduled by luci-scheduler."""
    git_repo = git_repo or self._default_git_repo(project)
    gitiles_host, gitiles_project = util.parse_gitiles_repo_url(git_repo)
    assert gitiles_host and gitiles_project, 'invalid repo %s' % git_repo

    # Do not add tags because recipe emulation results must not depend on tags.
    return build_pb2.Build(
        id=8945511751514863184,
        number=build_number,
        builder=build_pb2.BuilderID(
            project=project,
            bucket=bucket,
            builder=builder,
        ),
        created_by='user:luci-scheduler@appspot.gserviceaccount.com',
        create_time=timestamp_pb2.Timestamp(seconds=1527292217),
        input=build_pb2.Build.Input(
            gitiles_commit=common_pb2.GitilesCommit(
                host=gitiles_host,
                project=gitiles_project,
                ref=git_ref,
                id=revision,
            ),
        ),
    )

  def ci_build(self, *args, **kwargs):
    """Returns a typical buildbucket CI build scheduled by luci-scheduler.

    A shortcut for api.buildbucket.build(api.buildbucket.ci_build_message()).

    Usage:
        yield (api.test('basic') +
               api.buildbucket.ci_build(project='my-proj', builder='win'))
    """
    return self.build(self.ci_build_message(*args, **kwargs))


  def try_build_message(
      self,
      project='project',
      bucket='try',  # shortname.
      builder='builder',
      git_repo=None,
      change_number=123456,
      patch_set=7,
      revision=None,
      build_number=0):
    """Emulate typical buildbucket try build scheduled by CQ.

    Usage:

        yield (api.test('basic') +
               api.buildbucket.try_build(project='my-proj', builder='win'))
    """
    git_repo = git_repo or self._default_git_repo(project)
    git_host, git_project = util.parse_gitiles_repo_url(git_repo)

    gerrit_host = git_host
    gs_suffix = '.googlesource.com'
    if gerrit_host.endswith(gs_suffix):
      prefix = gerrit_host[:-len(gs_suffix)]
      if not prefix.endswith('-review'):
        gerrit_host = '%s-review%s' % (prefix, gs_suffix)

    # Do not add tags because recipe emulation results must not depend on tags.
    build = build_pb2.Build(
        id=8945511751514863184,
        number=build_number,
        builder=build_pb2.BuilderID(
            project=project,
            bucket=bucket,
            builder=builder,
        ),
        created_by='user:commit-bot@chromium.org',
        create_time=timestamp_pb2.Timestamp(seconds=1527292217),
        input=build_pb2.Build.Input(
            gerrit_changes=[
                common_pb2.GerritChange(
                    host=gerrit_host,
                    project=git_project,
                    change=change_number,
                    patchset=patch_set,
                ),
            ],
        ),
    )

    if revision:
      c = build.input.gitiles_commit
      c.host = git_host
      c.project = git_project
      c.id = revision

    return build

  def try_build(self, *args, **kwargs):
    """Emulates a typical buildbucket try build scheduled by CQ.

    Shortcut for api.buildbucket.build(api.buildbucket.try_build_message()).

    Usage:

        yield (api.test('basic') +
               api.buildbucket.try_build(project='my-proj', builder='win'))
    """
    return self.build(self.try_build_message(*args, **kwargs))

  def simulated_buildbucket_output(self, additional_build_parameters):
    buildbucket_output = {
        'build':{
          'parameters_json': json.dumps(additional_build_parameters)
        }
    }
    return self.step_data(
        'buildbucket.get',
        stdout=self.m.raw_io.output_text(json.dumps(buildbucket_output)))

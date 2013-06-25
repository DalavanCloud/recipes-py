# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import sys

from slave import recipe_api

class PlatformApi(recipe_api.RecipeApi):
  """
  Provides host-platform-detection properties.

  Mocks:
    name (str): A value equivalent to something that might be returned by
      sys.platform.
  """

  def __init__(self, **kwargs):
    super(PlatformApi, self).__init__(**kwargs)
    if self._mock is None:  # pragma: no cover
      self._platform = sys.platform
    else:
      self._platform = self._mock.get('name', sys.platform)

  @property
  def is_win(self):
    return self._platform.startswith(('cygwin', 'win'))

  @property
  def is_mac(self):
    return self._platform.startswith(('darwin', 'mac'))

  @property
  def is_linux(self):
    return self._platform.startswith('linux')

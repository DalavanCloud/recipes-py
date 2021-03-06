# Copyright 2019 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

from __future__ import print_function, absolute_import

import ast
import inspect
import json
import logging
import os
import posixpath
import sys
import types as stdlib_types

from cStringIO import StringIO

import astunparse

from google.protobuf import json_format as jsonpb
from google.protobuf import text_format as textpb

from . import config
from . import loader
from . import doc_markdown
from . import recipe_api
from . import types
from . import util
from . import doc_pb2 as doc


LOGGER = logging.getLogger(__name__)

RECIPE_ENGINE_BASE = os.path.dirname(
  os.path.dirname(os.path.abspath(__file__)))

join = posixpath.join
if sys.platform == 'win32':
  def _to_native(posix_path):
    return posix_path.replace(os.path.altsep, os.path.sep)
  def _to_posix(native_path):
    return native_path.replace(os.path.sep, os.path.altsep)
else:
  def _to_native(posix_path):
    return posix_path
  def _to_posix(native_path):
    return native_path


def _grab_ast(base_dir, relpath):
  try:
    with open(os.path.join(base_dir, _to_native(relpath)), 'rb') as f:
      return ast.parse(f.read(), relpath)
  except SyntaxError as ex:
    LOGGER.warn('skipping %s: bad syntax: %s', relpath, ex)
  except OSError as ex:
    LOGGER.warn('skipping %s: %s', relpath, ex)
  return None


def _unparse(node):
  buf = StringIO()
  astunparse.Unparser(node, buf)
  return buf.getvalue()


def _find_value_of(mod_ast, target):
  """Looks for an assignment to `target`, returning the assignment value ast
  node and the line number of the assignment.
  """
  for node in mod_ast.body:
    if isinstance(node, ast.Assign):
      if (len(node.targets) == 1 and
          isinstance(node.targets[0], ast.Name) and
          node.targets[0].id == target):
        return node.value, node.lineno
  return None, None


def _expand_mock_imports(*mock_imports):
  """Returns an expanded set of mock imports.

  mock_imports is expected to be a dict which looks like:
    {
      "absolute.module.name": SomeObject,
    }

  The objects in this dictionary will be the ones returned in the
  eval-compatible env dict. Think of it as a fake sys.modules. However, unlike
  sys.modules, mock_imports can be 'sparse'. For example, if mock_imports is:
    {
      'a.b': "foo",
      'a.c': "bar",
    }

  And the user does `import a`, this will make a fake object containing both .b
  and .c. However, this only works on leaf nodes. 'a.b' and 'a.b.d' would be an
  error (and this will raise ValueError if you attempt to do that).

  Returns a mock_imports, but with all the fake objects expanded.
  """
  combined_imports = {}
  for mi in mock_imports:
    combined_imports.update(mi)

  class expando(object):
    pass

  # expand combined_imports so it supports trivial lookups.
  expanded_imports = {}
  for dotted_name, obj in sorted(combined_imports.iteritems()):
    if dotted_name in expanded_imports:
      raise ValueError('nested mock imports! %r', dotted_name)
    toks = dotted_name.split('.')
    expanded_imports[dotted_name] = obj
    if isinstance(obj, stdlib_types.ModuleType):
      for name in (n for n in dir(obj) if not n.startswith('_')):
        expanded_imports[dotted_name+'.'+name] = getattr(obj, name)
    for i in range(len(toks)-1, 0, -1):
      partial = '.'.join(toks[:i])
      cur_obj = expanded_imports.setdefault(partial, expando())
      if not isinstance(cur_obj, expando):
        raise ValueError('nested mock imports! %r', partial)
      setattr(cur_obj, toks[i], expanded_imports[partial+'.'+toks[i]])

  return expanded_imports

ALL_IMPORTS = {}  # used in doc_test to ensure everything is actually importable
KNOWN_OBJECTS = {}

_decorator_imports = {
  'recipe_engine.util.returns_placeholder': util.returns_placeholder,
  'recipe_engine.recipe_api.non_step': recipe_api.non_step,
  'recipe_engine.recipe_api.infer_composite_step': (
    recipe_api.infer_composite_step)
}
KNOWN_OBJECTS.update(_decorator_imports)

_config_imports = {
  'recipe_engine.config.ConfigGroup': config.ConfigGroup,
  'recipe_engine.config.ConfigList': config.ConfigList,
  'recipe_engine.config.Set': config.Set,
  'recipe_engine.config.Dict': config.Dict,
  'recipe_engine.config.List': config.List,
  'recipe_engine.config.Single': config.Single,
  'recipe_engine.config.Static': config.Static,
  'recipe_engine.config.Enum': config.Enum,
}
KNOWN_OBJECTS.update(_config_imports)

_placeholder_imports = {
  'recipe_engine.util.OutputPlaceholder': util.OutputPlaceholder,
  'recipe_engine.util.InputPlaceholder': util.InputPlaceholder,
  'recipe_engine.util.Placeholder': util.Placeholder,
}
KNOWN_OBJECTS.update(_placeholder_imports)

_property_imports = {
  'recipe_engine.recipe_api.Property': recipe_api.Property,
}
KNOWN_OBJECTS.update(_property_imports)

_return_schema_imports = {
  'recipe_engine.config.ReturnSchema': config.ReturnSchema,
  'recipe_engine.config.ConfigGroupSchema': config.ConfigGroupSchema,
}
KNOWN_OBJECTS.update(_return_schema_imports)

_util_imports = {
  'recipe_engine.types.freeze': types.freeze,
}
KNOWN_OBJECTS.update(_util_imports)

_recipe_api_class_imports = {
  'recipe_engine.recipe_api.RecipeApi': recipe_api.RecipeApi,
  'recipe_engine.recipe_api.RecipeApiPlain': recipe_api.RecipeApiPlain,
}
KNOWN_OBJECTS.update(_recipe_api_class_imports)


def _parse_mock_imports(mod_ast, expanded_imports):
  """Parses a module ast node for import statements and resolves them against
  expanded_imports (such as you might get from _expand_mock_imports).

  If an import is not recognized, it is omitted from the returned dictionary.

  Returns a dictionary suitable for eval'ing a statement in mod_ast, with
  symbols from mod_ast's imports resolved to real objects, as per
  expanded_imports.
  """
  ret = {}

  for node in mod_ast.body:
    if isinstance(node, ast.Import):
      for alias in node.names:
        if alias.name in expanded_imports:
          ret[alias.asname or alias.name] = expanded_imports[alias.name]
    elif isinstance(node, ast.ImportFrom):
      if node.level == 0:
        for alias in node.names:
          fullname ='%s.%s' % (node.module, alias.name)
          if fullname in expanded_imports:
            ret[alias.asname or alias.name] = expanded_imports[fullname]

  return ret


def _apply_imports_to_unparsed_expression(exp_ast, imports):
  unparsed = _unparse(exp_ast).strip()
  try:
    return eval(unparsed, {'__builtins__': None}, imports)
  except (NameError, AttributeError):
    return unparsed


def _extract_classes_funcs(body_ast, relpath, imports, do_fixup=True):
  classes = {}
  funcs = {}

  for node in body_ast.body:
    if isinstance(node, ast.ClassDef):
      if not node.name.startswith('_'):
        classes[node.name] = parse_class(node, relpath, imports)
    elif isinstance(node, ast.FunctionDef):
      ok = (
        not node.name.startswith('_') or
        (node.name.startswith('__') and node.name.endswith('__'))
      )
      if ok:
        funcs[node.name] = parse_func(node, relpath, imports)

  if do_fixup:
    # frequently classes in a file inherit from other classes in the same file.
    # Do a best effort scan to re-attribute class bases when possible.
    for v in classes.itervalues():
      for i, b in enumerate(v.bases):
        if isinstance(b, str):
          if b in classes:
            v.bases[i] = classes[b]

  return classes, funcs


def parse_class(class_ast, relpath, imports):
  classes, funcs = _extract_classes_funcs(class_ast, relpath, imports, False)

  ret = doc.Doc.Class(
    relpath=relpath,
    name=class_ast.name,
    docstring=ast.get_docstring(class_ast) or '',
    lineno=class_ast.lineno,
    classes=classes,
    funcs=funcs,
  )

  for b in class_ast.bases:
    item = _apply_imports_to_unparsed_expression(b, imports)
    if isinstance(item, str):
      ret.bases.add(generic=item)
    else:
      ret.bases.add(known=item.__module__+'.'+item.__name__)

  return ret


def parse_deps(uv, mod_ast, relpath):
  ret = None

  DEPS, lineno = _find_value_of(mod_ast, 'DEPS')
  if DEPS:
    ret = doc.Doc.Deps(
      relpath=relpath,
      lineno=lineno,
    )
    spec = uv.normalize_deps_spec(ast.literal_eval(_unparse(DEPS)))
    for pkg, mod_name in sorted(spec.itervalues()):
      ret.module_links.add(package=pkg.name, name=mod_name)

  return ret


def extract_jsonish_assignments(mod_ast):
  """This extracts all single assignments where the target is a name, and the
  value is a simple 'jsonish' statement (aka python literal).

  The result is returned as a dictionary of name to the decoded literal.

  Example:
    Foo = "hello"
    Bar = [1, 2, "something"]
    Other, Things = range(2)  # not single assignment
    Bogus = object()  # not a python literal
    # returns: {"Foo": "hello", "Bar": [1, 2, "something"]}
  """
  ret = {}
  for node in mod_ast.body:
    if not isinstance(node, ast.Assign):
      continue
    if len(node.targets) != 1:
      continue
    if not isinstance(node.targets[0], ast.Name):
      continue
    try:
      ret[node.targets[0].id] = ast.literal_eval(node.value)
    except (KeyError, ValueError):
      pass
  return ret


def parse_parameter(param):
  assert isinstance(param, recipe_api.Property), type(param)
  default = None
  if param._default is not recipe_api.PROPERTY_SENTINEL:
    default = json.dumps(param._default)

  return doc.Doc.Parameter(
    docstring=param.help,
    kind=param.kind.schema_proto() if param.kind else None,
    default_json=default)


MOCK_IMPORTS_PARAMETERS = _expand_mock_imports(
  _property_imports, _config_imports)
ALL_IMPORTS.update(MOCK_IMPORTS_PARAMETERS)


def parse_parameters(mod_ast, relpath):
  parameters, lineno = _find_value_of(mod_ast, 'PROPERTIES')
  if not parameters:
    return None

  imports = _parse_mock_imports(mod_ast, MOCK_IMPORTS_PARAMETERS)
  imports.update(extract_jsonish_assignments(mod_ast))
  data = eval(_unparse(parameters), imports)
  if not data:
    return None

  for k, v in sorted(data.iteritems()):
    data[k] = parse_parameter(v)

  return doc.Doc.Parameters(relpath=relpath, lineno=lineno, parameters=data)


def parse_func(func_node, relpath, imports):
  ret = doc.Doc.Func(
    name=func_node.name,
    relpath=relpath,
    lineno=func_node.lineno,
    docstring=ast.get_docstring(func_node) or '',
  )

  for exp in func_node.decorator_list:
    item = _apply_imports_to_unparsed_expression(exp, imports)
    if isinstance(item, str):
      ret.decorators.add(generic=item)
    else:
      ret.decorators.add(known=item.__module__+'.'+item.__name__)

  ret.signature = _unparse(func_node.args).strip()
  return ret


MOCK_IMPORTS_RETURN_SCHEMA = _expand_mock_imports(
  _return_schema_imports, _config_imports)
ALL_IMPORTS.update(MOCK_IMPORTS_RETURN_SCHEMA)


def parse_return_schema(mod_ast, relpath):
  imports = _parse_mock_imports(mod_ast, MOCK_IMPORTS_RETURN_SCHEMA)
  schema, lineno = _find_value_of(mod_ast, 'RETURN_SCHEMA')
  if not schema:
    return None
  schema = eval(_unparse(schema), imports)
  if not schema:
    return None
  return doc.Doc.ReturnSchema(relpath=relpath, lineno=lineno,
                              schema=schema.schema_proto())


MOCK_IMPORTS_RECIPE = _expand_mock_imports(
 _util_imports, _decorator_imports, _placeholder_imports)
ALL_IMPORTS.update(MOCK_IMPORTS_RECIPE)


def parse_recipe(uv, base_dir, relpath, recipe_name):
  recipe = _grab_ast(base_dir, relpath)
  if not recipe:
    return None
  classes, funcs = _extract_classes_funcs(recipe, relpath, MOCK_IMPORTS_RECIPE)
  funcs.pop('GenTests', None)

  # TODO(iannucci): parse RequireClients

  return doc.Doc.Recipe(
    name=recipe_name,
    relpath=relpath,
    docstring=ast.get_docstring(recipe) or '',
    deps=parse_deps(uv, recipe, relpath),
    parameters=parse_parameters(recipe, relpath),
    return_schema=parse_return_schema(recipe, relpath),
    classes=classes,
    funcs=funcs,
  )


MOCK_IMPORTS_MODULE = _expand_mock_imports(
  _recipe_api_class_imports, _decorator_imports, _placeholder_imports)
ALL_IMPORTS.update(MOCK_IMPORTS_MODULE)


def parse_module(uv, base_dir, relpath, mod_name):
  native_relpath = _to_native(relpath)

  api_relpath = relpath + '/api.py'
  api = _grab_ast(base_dir, _to_native(api_relpath))
  if not api:
    return None

  init_relpath = os.path.join(native_relpath, '__init__.py')
  init = _grab_ast(base_dir, init_relpath)
  if not init:
    return None

  imports = _parse_mock_imports(api, MOCK_IMPORTS_MODULE)
  classes, funcs = _extract_classes_funcs(api, api_relpath, imports)

  api_class = None
  for name, val in sorted(classes.iteritems()):
    if any(b.known in _recipe_api_class_imports for b in val.bases):
      api_class = classes.pop(name)
      break
  if not api_class:
    LOGGER.error('could not determine main RecipeApi class: %r', relpath)
    return None

  return doc.Doc.Module(
    name=mod_name,
    relpath=relpath,
    docstring=ast.get_docstring(api) or '',
    api_class=api_class,
    classes=classes,
    funcs=funcs,
    deps=parse_deps(uv, init, init_relpath),
    parameters=parse_parameters(init, init_relpath),
  )


def parse_package(uv, base_dir, spec):
  ret = doc.Doc.Package(project_id=spec.project_id)
  ret.specs[spec.project_id].CopyFrom(spec)
  for dep, pkg in uv.package.deps.iteritems():
    ret.specs[dep].CopyFrom(pkg.repo_spec.spec_pb())

  readme = join(base_dir, 'README.recipes.intro.md')
  if os.path.isfile(readme):
    with open(readme, 'rb') as f:
      ret.docstring = f.read()

  mod_base = posixpath.relpath(uv.module_dir, base_dir)
  for mod_name in uv.loop_over_recipe_modules():
    relpath = join(mod_base, mod_name)
    mod = parse_module(uv, base_dir, relpath, mod_name)
    if mod:
      ret.recipe_modules[mod_name].CopyFrom(mod)

  for recipe_path, recipe_name in uv.loop_over_recipes():
    relpath = posixpath.relpath(recipe_path, base_dir)
    recipe = parse_recipe(uv, base_dir, relpath, recipe_name)
    if recipe:
      ret.recipes[recipe_name].CopyFrom(recipe)

  return ret


RECIPE_ENGINE_URL = 'https://chromium.googlesource.com/infra/luci/recipes-py'


def _set_known_objects(base):
  source_cache = {}

  def _add_it(key, fname, target):
    relpath = os.path.relpath(fname, RECIPE_ENGINE_BASE)
    for node in source_cache[fname].body:
      if isinstance(node, ast.ClassDef) and node.name == target:
        # This is a class definition in the form of:
        #   def Target(...)
        base.known_objects[key].klass.CopyFrom(parse_class(node, relpath, {}))
        return
      elif isinstance(node, ast.FunctionDef) and node.name == target:
        # This is a function definition in the form of:
        #   def Target(...)
        base.known_objects[key].func.CopyFrom(parse_func(node, relpath, {}))
        return
      elif isinstance(node, ast.Assign) and node.targets[0].id == target:
        # This is an alias in the form of:
        #   Target = RealImplementation
        _add_it(key, fname, node.value.id)
        return

    raise ValueError('could not find %r in %r' % (key, relpath))

  for k, v in KNOWN_OBJECTS.iteritems():
    base.known_objects[k].url = RECIPE_ENGINE_URL
    _, target = k.rsplit('.', 1)
    fname = inspect.getsourcefile(v)
    if fname not in source_cache:
      # we load and cache the whole source file so that ast.parse gets the right
      # line numbers for all the definitions.
      source_lines, _ = inspect.findsource(v)
      source_cache[fname] = ast.parse(''.join(source_lines), fname)

    _add_it(k, fname, target)


def add_subparser(parser):
  doc_kinds=('binarypb', 'jsonpb', 'textpb', 'gen', 'markdown')
  helpstr = (
    'List all known modules reachable from the current package, with their '
    'documentation.'
  )
  doc_p = parser.add_parser(
    'doc', help=helpstr, description=helpstr)
  doc_p.add_argument('recipe', nargs='?',
                     help='Restrict documentation to this recipe')
  doc_p.add_argument(
    '--kind', default='jsonpb', choices=doc_kinds,
    help=(
      'Output this kind of documentation. `gen` will write the standard '
      'README.recipes.md file. All others output to stdout'))

  doc_p.set_defaults(func=main)


def regenerate_docs(universe_view, package_deps):
  spec = universe_view.package.repo_spec.spec_pb()
  base_dir = universe_view.package.repo_root
  node = parse_package(universe_view, base_dir, spec)
  _set_known_objects(node)

  readme = os.path.join(
    package_deps.root_package.recipes_dir, 'README.recipes.md')
  with open(readme, 'wb') as f:
    doc_markdown.Emit(doc_markdown.Printer(f), node)


def main(package_deps, args):
  universe = loader.RecipeUniverse(package_deps, args.package)
  universe_view = loader.UniverseView(universe, package_deps.root_package)

  logging.basicConfig()

  # defer to regenerate_docs for consistency between train and 'doc --kind gen'
  if args.kind == 'gen':
    print('Generating README.recipes.md')
    regenerate_docs(universe_view, package_deps)
    return 0

  spec = universe_view.package.repo_spec.spec_pb()
  base_dir = universe_view.package.repo_root
  if spec.recipes_path:
    base_dir = join(base_dir, spec.recipes_path)

  if args.recipe:
    recipe_fullpath = universe_view.find_recipe(args.recipe)
    relpath = _to_posix(os.path.relpath(recipe_fullpath, base_dir))
    node = parse_recipe(universe_view, base_dir, relpath, args.recipe)
  else:
    node = parse_package(universe_view, base_dir, spec)

  _set_known_objects(node)

  if args.kind == 'jsonpb':
    sys.stdout.write(jsonpb.MessageToJson(
      node, including_default_value_fields=True,
      preserving_proto_field_name=True))
  elif args.kind == 'binarypb':
    sys.stdout.write(node.SerializeToString())
  elif args.kind == 'textpb':
    sys.stdout.write(textpb.MessageToString(node))
  elif args.kind == 'markdown':
    doc_markdown.Emit(doc_markdown.Printer(sys.stdout), node)
  else:
    raise NotImplementedError('--kind=%s' % args.kind)

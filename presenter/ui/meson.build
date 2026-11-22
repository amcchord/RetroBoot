# presenter/ui - rbui, the classic (Mac OS 8/9 Platinum) panel UI library.
# Owned by the classic-ui workstream; included from presenter/meson.build with
# subdir('ui'). Provides:
#   rbui_lib  static library: rbui plus the vendored cJSON and the one
#             stb_image_write implementation (do not compile either again)
#   rbui_dep  link rbui_lib; include dirs for rbui.h, cJSON.h and
#             stb_image_write.h
#   rbui-render (tool) and the rbui unit/golden tests (meson test --suite rbui)

rbui_cc = meson.get_compiler('c')
rbui_libm = rbui_cc.find_library('m', required : false)

rbui_tp_inc = include_directories('third_party', 'third_party/cjson')
rbui_inc = include_directories('include')
rbui_src_inc = include_directories('src')

# Vendored third-party code (cJSON 1.7.19, MIT; stb_image_write 1.16, public
# domain) builds with its own warnings silenced.
rbui_tp_lib = static_library('rbui_thirdparty',
  'third_party/cjson/cJSON.c', 'src/stb_impl.c',
  include_directories : rbui_tp_inc,
  c_args : ['-w'])

rbui_lib = static_library('rbui',
  'src/assets.c', 'src/doc.c', 'src/gfx.c', 'src/input.c', 'src/layout.c', 'src/platinum.c',
  'src/png.c', 'src/rbui.c', 'src/feedback.c', 'src/render.c', 'src/fonts_data.c', 'src/icons_data.c',
  include_directories : [rbui_inc, rbui_src_inc, rbui_tp_inc],
  link_whole : rbui_tp_lib,
  dependencies : rbui_libm)

rbui_dep = declare_dependency(
  link_with : rbui_lib,
  include_directories : [rbui_inc, rbui_tp_inc],
  dependencies : rbui_libm)

rbui_render = executable('rbui-render', 'tools/rbui-render.c',
  dependencies : rbui_dep,
  install : false)

rbui_test = executable('test_rbui', 'tests/test_rbui.c',
  include_directories : rbui_src_inc,
  dependencies : rbui_dep)
test('rbui', rbui_test,
  args : [meson.current_source_dir() / 'examples'],
  suite : 'rbui',
  timeout : 300)

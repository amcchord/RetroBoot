project('rb-presenter', 'c',
  version : '0.2.0',
  license : 'proprietary',
  meson_version : '>= 1.0',
  default_options : ['c_std=gnu11', 'warning_level=2', 'buildtype=debugoptimized'])

fs = import('fs')
cc = meson.get_compiler('c')

git = run_command('git', '-C', meson.current_source_dir(), 'describe', '--always', '--dirty', '--abbrev=10',
                  check : false)
rb_version = meson.project_version()
if git.returncode() == 0
  rb_version = '@0@+@1@'.format(rb_version, git.stdout().strip())
elif fs.exists('.gitrev')
  # Written by tools/sync.sh when building outside a Git checkout.
  rb_version = '@0@+@1@'.format(rb_version, fs.read('.gitrev').strip())
endif
add_project_arguments('-D_GNU_SOURCE', '-DRB_VERSION="@0@"'.format(rb_version), language : 'c')

libdrm = dependency('libdrm')
libudev = dependency('libudev')
libinput = dependency('libinput', version : '>= 1.19')
xkbcommon = dependency('xkbcommon')
libm = cc.find_library('m', required : true)

# Classic-ui panel library (presenter/ui, owned by the classic-ui workstream):
# rbui_dep also exports the vendored cJSON and stb_image_write (implementation
# compiled once inside rbui_lib).
if not fs.exists('ui/meson.build')
  error('presenter/ui (classic-ui, wave2/classic-ui) is required; merge it into this tree')
endif
subdir('ui')
ui_dep = rbui_dep

src_inc = include_directories('src')
# The view of Startup Details (src/diagview.c) draws with classic-ui's MIT
# bitmap fonts: their tables (ui/src/fonts_data.c) are compiled into the
# programs that do not link rbui (rb-splash, rb-bootdiag).
ui_src_inc = include_directories('ui/src')

# Splash/display core: libdrm + libudev only (also linked into rb-splash).
core_lib = static_library('rbcore',
  'src/log.c', 'src/loop.c', 'src/region.c', 'src/checker.c', 'src/scale.c', 'src/xform.c', 'src/drm.c', 'src/vt.c',
  'src/startup.c', 'src/installer_splash.c', 'src/splash.c', 'src/rotation.c', 'src/framelog.c', 'src/diagview.c',
  include_directories : ui_src_inc,
  dependencies : [libdrm, libudev, libm])
core_dep = declare_dependency(link_with : core_lib, include_directories : src_inc,
  dependencies : [libdrm, libudev, libm])

app_lib = static_library('rbapp',
  'src/content.c', 'src/switchhold.c', 'src/keymap.c', 'src/gemkbd.c', 'src/guest.c', 'src/ctl.c', 'src/input.c',
  'src/proto.c', 'src/pointer.c', 'src/sample.c',
  'src/png.c', 'src/remote.c', 'src/badge.c', 'src/osd.c', 'src/touch.c', 'src/presenter.c',
  dependencies : [core_dep, ui_dep, libinput, xkbcommon, libm])
app_dep = declare_dependency(link_with : app_lib,
  dependencies : [core_dep, ui_dep, libinput, xkbcommon, libm])

bindir = get_option('prefix') / 'lib/retroboot/bin'

executable('rb-presenter', 'src/main.c',
  dependencies : app_dep,
  install : true,
  install_dir : bindir)

executable('rb-splash', 'src/splash_main.c', 'ui/src/fonts_data.c',
  include_directories : ui_src_inc,
  dependencies : core_dep,
  install : true,
  install_dir : bindir)

# Startup Details collector (ARCHITECTURE 16): libc only, also in the initramfs.
bootdiag_lib = static_library('rbbootdiag', 'src/bootdiag_lib.c', include_directories : src_inc)
executable('rb-bootdiag', 'src/bootdiag.c', 'src/diagview.c', 'src/log.c', 'src/loop.c', 'src/scale.c', 'src/region.c',
  'ui/src/fonts_data.c',
  include_directories : [src_inc, ui_src_inc],
  link_with : bootdiag_lib,
  install : true,
  install_dir : bindir)

install_data('packaging/systemd/retroboot-presenter.service',
  install_dir : get_option('prefix') / 'lib/systemd/system')
install_data('packaging/initramfs/hooks/retroboot-splash',
  install_dir : '/usr/share/initramfs-tools/hooks', install_mode : 'rwxr-xr-x')
install_data('packaging/initramfs/scripts/init-top/retroboot-splash',
  install_dir : '/usr/share/initramfs-tools/scripts/init-top', install_mode : 'rwxr-xr-x')
install_data('packaging/initramfs/scripts/init-bottom/retroboot-splash',
  install_dir : '/usr/share/initramfs-tools/scripts/init-bottom', install_mode : 'rwxr-xr-x')
install_data('packaging/systemd/retroboot-bootdiag.service',
  install_dir : get_option('prefix') / 'lib/systemd/system')
install_data('packaging/initramfs/hooks/retroboot-bootdiag',
  install_dir : '/usr/share/initramfs-tools/hooks', install_mode : 'rwxr-xr-x')
install_data('packaging/initramfs/scripts/init-top/retroboot-bootdiag',
  install_dir : '/usr/share/initramfs-tools/scripts/init-top', install_mode : 'rwxr-xr-x')
install_data('packaging/initramfs/scripts/init-bottom/retroboot-bootdiag',
  install_dir : '/usr/share/initramfs-tools/scripts/init-bottom', install_mode : 'rwxr-xr-x')

foreach t : ['test_scale', 'test_content', 'test_keymap', 'test_proto', 'test_bootfb', 'test_xform', 'test_startup', 'test_remote', 'test_osd', 'test_anchor',
           'test_touch', 'test_splash_policy', 'test_framelog', 'test_switchhold', 'test_drm_select', 'test_damage', 'test_gemkbd',
           'test_isplash', 'test_diagview', 'test_pointer', 'test_sample']
  test(t, executable(t, 'tests/' + t + '.c', dependencies : app_dep, include_directories : src_inc),
       suite : 'presenter')
endforeach
test('test_bootdiag', executable('test_bootdiag', 'tests/test_bootdiag.c', dependencies : app_dep,
     include_directories : src_inc, link_with : bootdiag_lib), suite : 'presenter')

# Lab: render the installer startup screen (4.4.2) to PNG; not installed.
executable('isplash-render', 'tools/isplash_render.c', dependencies : app_dep, include_directories : src_inc)

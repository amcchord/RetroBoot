# Standalone build for local iteration (macOS clang or Linux gcc).
# The product build is meson (meson.build, included from presenter/meson.build).
#   make            library, rbui-render and tests in ./build-make
#   make test       run the unit/golden tests
#   make asan       the same with AddressSanitizer + UBSan in ./build-asan
#   make examples   re-render examples/rendered/*.png (1x and 2x)
CC ?= cc
OUT ?= build-make
OPT ?= -O2 -g
CFLAGS_ALL = -std=gnu11 $(OPT) -Wall -Wextra -Iinclude -Isrc -Ithird_party -Ithird_party/cjson $(EXTRA)

LIB_SRC = src/assets.c src/doc.c src/gfx.c src/input.c src/layout.c src/platinum.c src/png.c src/rbui.c src/feedback.c src/render.c \
          src/fonts_data.c src/icons_data.c
TP_SRC = third_party/cjson/cJSON.c src/stb_impl.c
TESTS = test_rbui
LIB = $(OUT)/librbui.a

LIB_OBJ = $(LIB_SRC:%.c=$(OUT)/%.o)
TP_OBJ = $(TP_SRC:%.c=$(OUT)/%.o)

all: $(LIB) $(OUT)/rbui-render $(OUT)/rbui-cases $(OUT)/test_rbui

$(OUT)/third_party/%.o: third_party/%.c
	@mkdir -p $(dir $@)
	$(CC) -std=gnu11 $(OPT) -w $(EXTRA) -Ithird_party -Ithird_party/cjson -c $< -o $@

$(OUT)/src/stb_impl.o: src/stb_impl.c
	@mkdir -p $(dir $@)
	$(CC) -std=gnu11 $(OPT) -w $(EXTRA) -Ithird_party -c $< -o $@

$(OUT)/%.o: %.c src/rbui_internal.h src/rbui_names.h src/font.h src/icons.h include/rbui.h
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS_ALL) -c $< -o $@

$(LIB): $(LIB_OBJ) $(TP_OBJ)
	rm -f $@
	ar rcs $@ $^

$(OUT)/rbui-render: tools/rbui-render.c $(LIB)
	$(CC) $(CFLAGS_ALL) $< $(LIB) -lm -o $@

# Platinum controls at reference-capture coordinates (web/dev/platinum_check.py --panel).
$(OUT)/rbui-cases: tools/rbui-cases.c $(LIB)
	$(CC) $(CFLAGS_ALL) $< $(LIB) -lm -o $@

$(OUT)/test_rbui: tests/test_rbui.c $(LIB)
	$(CC) $(CFLAGS_ALL) $< $(LIB) -lm -o $@

test: all symcheck
	$(OUT)/test_rbui examples

# Every exported symbol must carry an rbui/cJSON/stbi prefix (see src/rbui_names.h).
symcheck: $(LIB)
	@bad=$$(nm -g $(LIB) 2>/dev/null | awk 'NF==3 && $$2 ~ /^[TDBSC]$$/ {print $$3}' | sed 's/^_//' | grep -v '^rbui_\|^cJSON\|^stbi\|^_' || true); \
	if [ -n "$$bad" ]; then echo "unprefixed symbols in $(LIB): $$bad"; exit 1; else echo "symcheck ok"; fi

asan:
	$(MAKE) OUT=build-asan OPT="-O1 -g -fno-omit-frame-pointer" EXTRA="-fsanitize=address,undefined -fno-sanitize-recover=undefined" test

examples: $(OUT)/rbui-render
	tools/render-examples.sh $(OUT)/rbui-render

clean:
	rm -rf build-make build-asan

.PHONY: all test symcheck asan examples clean

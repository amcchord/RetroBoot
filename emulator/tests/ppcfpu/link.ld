/* fpconf: ROM image at the mac99 PROM base (vectors at 0xfff00000, MSR[IP]),
 * code from 0xfff20000, zero-initialised data in RAM from 0x00100000. */
OUTPUT_FORMAT("elf32-powerpc")
OUTPUT_ARCH(powerpc:common)
ENTRY(_start)
SECTIONS
{
  . = 0xfff00000;
  .vectors : { KEEP(*(.vectors)) }
  /* 0xfff04000-0xfff0ffff is the mac99 NVRAM window (it_shift 1), not ROM */
  . = 0xfff20000;
  .text : { *(.text*) }
  .rodata : { *(.rodata*) *(.sdata2*) *(.sbss2*) }
  .got : { *(.got*) }
  .data : { *(.data*) *(.sdata*) }
  ASSERT(SIZEOF(.data) == 0, "fpconf: no initialised RAM data allowed")
  . = 0x00100000;
  .bss (NOLOAD) : { __bss_start = .; *(.bss*) *(.sbss*) *(COMMON) . = ALIGN(4); __bss_end = .; }
  /DISCARD/ : { *(.comment) *(.eh_frame*) *(.note*) *(.gnu.attributes) }
}

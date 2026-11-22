ENTRY(_start)
SECTIONS {
  . = 0x700;
  .vectors : { *(.vectors) }
  . = 0x100000;
  .text : { *(.text) }
}

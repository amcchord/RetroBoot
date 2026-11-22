/* Virtual terminal: put the active VT in KD_GRAPHICS and K_OFF so the kernel
 * console never draws (text, cursor) or consumes keys (VT switching, SAK). */
#pragma once

int rb_vt_take(void);     /* returns 0 on success (or no VT support) */
void rb_vt_restore(void); /* debug only: back to text mode and the saved kbd mode */
int rb_vt_number(void);

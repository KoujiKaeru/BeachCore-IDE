; ============================================================
; REVERSE COPY
; ============================================================
; TECHNIQUE: SMC with two independently moving pointers
; ------------------------------------------------------------
; Copies 8 bytes from 0x200..0x207 into 0x300..0x307,
; but in REVERSE ORDER. The source pointer starts at the END
; of the array (0x207) and walks backwards. The destination
; pointer starts at the beginning (0x300) and walks forward.
;
; Both pointers are implemented with SMC:
;   LD 0x207  is at 0x010  ->  operand (low byte) at 0x011
;   ST 0x300  is at 0x012  ->  operand (low byte) at 0x013
;
; INIT seeds:
;   0x011 = 0x07  (start reading from offset 7 = 0x207)
;   0x013 = 0x00  (start writing to offset 0  = 0x300)
;
; Each loop: read pointer decrements, write pointer increments.
;
; SETUP: Pre-load 8 bytes into 0x200..0x207.
;        After running, 0x300..0x307 will hold them reversed.
;
; MEMORY MAP:
;   0x001 - loop counter
;   0x011 - SMC PATCH: LD read address low byte (counts DOWN)
;   0x013 - SMC PATCH: ST write address low byte (counts UP)
;   0x200 - source array (load 8 bytes here)
;   0x300 - destination array (reversed copy appears here)
; ============================================================

INIT:
    LDI 0x08
    ST  0x001           ; counter = 8
    LDI 0x07
    ST  0x011           ; patch LD: start at 0x207 (end of source)
    LDI 0x00
    ST  0x013           ; patch ST: start at 0x300 (start of dest)

LOOP:
    LD  0x001
    JZ  DONE

    LD  0x207           ; Read from source (0x011 is patched, walks backward)
    ST  0x300           ; Write to dest    (0x013 is patched, walks forward)

    LD  0x011           ; Source pointer: move backward
    DEC		      ; (decrement)
    ST  0x011

    LD  0x013           ; Dest pointer: move forward
    INC                 ; (increment)
    ST  0x013

    LD  0x001
    DEC
    ST  0x001           ; Decrement counter

    JMP LOOP

DONE:
    HALT

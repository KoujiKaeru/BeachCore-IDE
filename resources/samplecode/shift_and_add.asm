; ============================================================
; 8-BIT MULTIPLY (via shift-and-add)
; ============================================================
; TECHNIQUE: Software multiply, shift-and-add
; ------------------------------------------------------------
; This CPU has no MUL instruction. We implement multiplication
; using the shift-and-add algorithm
;
;
; Change the LDI values in INIT to multiply different numbers.
; Note: result must fit in 8 bits (max ~15*15=225 is safe).
; Values that overflow 255 will produce a truncated result.
;
; MEMORY MAP:
;   0x100 - operand A
;   0x101 - operand B
;   0x102 - counter
;   0x103 - final product output (watch this)
; ============================================================

INIT:
    LDI 	0x0E
    ST  	0x100	; A = 14
    LDI 	0x12
    ST  	0x101	; B = 18
    LDI 	0x08
    ST  	0x102	; counter = 8
    LDI 	0x00
    ST  	0x103	; product = 0

LOOP:
    LD  	0x102	; Check the counter
    JZ  	DONE	; Counter reached zero - done

    LD  	0x101	; Check LSB of B
    ANDI 0x01
    JZ   SHIFT	; Skip add if the LSB is 0

    LD  	0x103	; product = A + product
    ADD 	0x100
    ST  	0x103

SHIFT:
    LD 	0x100	; Shift A left by 1 bit
    SHL
    ST 	0x100
    LD 	0x101	; Shift B right by 1 bit
    SHR
    ST 	0x101

    LD 	0x102	; Decrement counter
    DEC
    ST 	0x102
    JMP 	LOOP

DONE:
    HALT
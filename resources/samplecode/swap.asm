; SWAP (USING PUSH AND POP)
; We need to swap 2 variables in RAM, so we use the stack as temporary storage
; 
; Algorithm:
; temp = var1
; var1 = var2
; var2 = temp
;
; 0x100 - Variable 1 (watch both of these)
; 0x101 - Variable 2
; 0xEFF - Stack (Watch this to see the value being pushed and popped)

.org 0x000
RESET:
	LDI 0xAA		; Variable 1
	ST 0x100
	LDI 0x55		; Variable 2
	ST 0x101
	JMP START

.org 0x030
START:
	LD 0x100		; Load our first variable and push to the stack
	PUSH
	LD 0x101		; Load our second variable and overwrite first variable
	ST 0x100
	POP		; Pop first variable off stack and overwrite second variable
	ST 0x101
	HALT
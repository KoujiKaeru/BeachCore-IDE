; CMPI TEST
; The basic for loop is written
; for(i = 0; i < 10; i++)
; The for loop should run 10 times before halting

.org 0x000
RESET:
	LDI 0		; Counter = 0
	ST  0x100
	JMP START

.org 0x050
START:
	LD 0x100
	INC
	ST 0x100
	CMPI 10		; Compare accumulator with 10
	JC END		; Halt if the accumulator >= 10
	JMP START	; Otherwise jump back to the start

END:
	HALT
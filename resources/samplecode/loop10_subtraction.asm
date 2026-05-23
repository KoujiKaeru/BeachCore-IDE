; SUBTRACTION TEST
; The basic for loop is written
; for(i = 20; i > 0; i -= 2)
; The for loop should run 10 times before halting

.org 0x000
RESET:
	LDI 20		; Counter = 10
	ST  0x100
	JMP START

.org 0x050
START:
	LD 0x100
	SUBI 0x02	; Counter = Counter - 2
	ST 0x100
	JZ END		; Halt if the result is 0
	JMP START	; Otherwise jump back to the start

END:
	HALT
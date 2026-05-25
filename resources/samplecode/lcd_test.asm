; Character to LCD Example code with pointer
; 0xFFE - LCD Data (D7-D0)
; 0xFFF - LCD RS, R/W, E (LSB of I/O)
; 0x100 - Delay counter

; Watch 0xFF0-0xFFF for the data/instruction input and flags input into the LCD
; Watch 0x100 for the delay counter
; Watch OxEF0-0xEFF to watch the call stack

.org 0x000
RESET:
	JMP START

.org 0x010
my_string: .asciiz "Hello, world!"	; Create a string to pring to LCD
fn_set: .byte 0x38		; LCD function set instruction
disp_on: .byte 0x0E		; LCD display on instruction
entry_mode: .byte 0x06		; LCD entry mode set instruction
clear: .byte 0x01			; LCD clear instruction
delay_time: .byte 0x0F		; Delay time (set this to whatever you want)

.org 0x030
START:
	LD delay_time		; Set up delay counter
	ST 0x100
	LD fn_set		; Initialize the LCD
	CALL WRITE_IN
	LD disp_on
	CALL WRITE_IN
	LD entry_mode
	CALL WRITE_IN
	LD clear
	CALL WRITE_IN
	LDP my_string		; Start at beginning of string
	JMP LOOP

WRITE_IN:
	ST 0xFFE			; Load instruction/data to data bus
	LDI 0x00
	ST 0xFFF
	CALL DELAY
	LDI 0x01			; Pulse enable pin
	ST 0xFFF
	CALL DELAY
	LDI 0x00
	ST 0xFFF
	CALL DELAY
	RET

DELAY:
	LD 0x100			; Decrement the delay counter
	DEC
	ST 0x100
	JNZ DELAY		; Keep doing this until we read zero
	LD delay_time		; Restore the delay counter
	ST 0x100
	RET

WRITE_CHAR:
	ST 0xFFE			; Load instruction/data to data bus
	LDI 0x02
	ST 0xFFF
	CALL DELAY
	LDI 0x03			; Pulse enable pin
	ST 0xFFF
	CALL DELAY
	LDI 0x02
	ST 0xFFF
	CALL DELAY
	RET
	
LOOP:
	LD [pr]			; Check if we have reached the null terminator
	JZ END			; Halt if we are done writing the string
	CALL WRITE_CHAR		; Otherwise write char to data bus of LCD
	ADDPI 0x01		; Increment the pointer
	JMP LOOP
	

END: HALT
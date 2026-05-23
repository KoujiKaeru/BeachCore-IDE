# CPU & ASSEMBLER CLASSES
#-----------------------------------------------------------------------

class CPU:
    def __init__(self):
        self.mem = bytearray(4096)
        self.breakpoints = set()
        self.skip_breakpoint = False
        self.reset()

    def reset(self):
        self.pc, self.acc, self.c, self.z, self.ie, self.in_isr, self.pc_save, self.cycles = 0, 0, 0, 0, 0, 0, 0, 0
        self.halted = False
        self.skip_breakpoint = False

    def step(self):
        if self.halted: return
        
        op_byte = self.mem[self.pc]
        primary_op = (op_byte >> 4) & 0xF
        sub_op = op_byte & 0xF
        
        # Implied Instructions
        if primary_op == 0x0:
            self.pc = (self.pc + 1) & 0xFFF
            if sub_op == 0x00: pass 
            elif sub_op == 0x01: 
                self.c = self.acc & 1
                self.acc >>= 1
            elif sub_op == 0x02: 
                res = self.acc << 1
                self.c = (res >> 8) & 1
                self.acc = res & 0xFF
            elif sub_op == 0x03: self.ie = 1 
            elif sub_op == 0x04: self.ie = 0 
            elif sub_op == 0x05: 
                self.pc = self.pc_save
                self.in_isr = 0
                self.ie = 1
            elif sub_op == 0x06: self.halted = True 
            elif sub_op == 0x07: 
                self.acc = (~self.acc) & 0xFF
                self.c = bin(self.acc).count("1") % 2
            elif sub_op == 0x08:
                res = self.acc + 1
                self.c = (res >> 8) & 1
                self.acc = res & 0xFF
            elif sub_op == 0x09:
                res = self.acc - 1
                self.c = int(self.acc >= 1)
                self.acc = res & 0xFF
            self.cycles += 3
            self.z = 1 if self.acc == 0 else 0

        # Immediate Instructions
        elif primary_op == 0x1:
            imm = self.mem[(self.pc + 1) & 0xFFF]
            self.pc = (self.pc + 2) & 0xFFF
            if sub_op == 0x0: self.acc = imm 
            elif sub_op == 0x2: self.acc &= imm; self.c = bin(self.acc).count("1") % 2
            elif sub_op == 0x3: self.acc |= imm; self.c = bin(self.acc).count("1") % 2
            elif sub_op == 0x4: self.acc ^= imm; self.c = bin(self.acc).count("1") % 2
            elif sub_op == 0x5: 
                res = self.acc + imm
                self.c = (res >> 8) & 1
                self.acc = res & 0xFF
            elif sub_op == 0x6:
                res = self.acc - imm
                self.c = (self.acc >= imm)
                self.acc = res & 0xFF
            self.cycles += 5
            self.z = 1 if self.acc == 0 else 0

        # Address Instructions
        else:
            addr12 = (sub_op << 8) | self.mem[(self.pc + 1) & 0xFFF]
            next_pc = (self.pc + 2) & 0xFFF

            if primary_op == 0x2: self.acc &= self.mem[addr12]; self.c = bin(self.acc).count("1") % 2
            elif primary_op == 0x3: self.acc |= self.mem[addr12]; self.c = bin(self.acc).count("1") % 2
            elif primary_op == 0x4: self.acc ^= self.mem[addr12]; self.c = bin(self.acc).count("1") % 2
            elif primary_op == 0x5: 
                res = self.acc + self.mem[addr12]
                self.c = (res >> 8) & 1
                self.acc = res & 0xFF
            elif primary_op == 0x6:
                res = self.acc - self.mem[addr12]
                self.c = (self.acc >= self.mem[addr12])
                self.acc = res & 0xFF
            elif primary_op == 0x7: self.acc = self.mem[addr12] 
            elif primary_op == 0x8: self.mem[addr12] = self.acc 
            elif primary_op == 0x9: next_pc = addr12 
            elif primary_op == 0xA: 
                if self.z: next_pc = addr12
            elif primary_op == 0xB: 
                if not self.z: next_pc = addr12
            elif primary_op == 0xC: 
                if self.c: next_pc = addr12
            elif primary_op == 0xD: 
                if not self.c: next_pc = addr12
            
            self.pc = next_pc
            self.cycles += 7
            self.z = 1 if self.acc == 0 else 0

    def trigger_interrupt(self):
        if self.ie and not self.in_isr:
            self.pc_save = self.pc
            self.in_isr = 1
            self.ie = 0
            self.pc = 0x008
            self.halted = False

class Assembler:

    # Operations w/ op-codes
    IMPLIED = {
        'NOP':0x00, 'SHR':0x01, 'SHL':0x02, 'EI':0x03, 
        'DI':0x04, 'RETI':0x05, 'HALT':0x06, 'INV':0x07,
        'INC': 0x08, 'DEC': 0x09
    }
    IMMEDIATE = {
        'LDI':0x10, 'ANDI':0x12, 'ORI':0x13, 'XORI':0x14, 'ADDI':0x15, 'SUBI': 0x16
    }
    ADDRESS = {
        'AND':0x2, 'OR':0x3, 'XOR':0x4, 'ADD':0x5, 
        'SUB':0x6,'LD':0x7, 'ST':0x8, 'JMP':0x9, 
        'JZ':0xA, 'JNZ':0xB, 'JC':0xC, 'JNC':0xD
    }

    def assemble(self, source):

        # TODO: Show error message in IDE if syntactically incorrect
        # Array of bytes to store final machine code
        # Keep track of labels and their addresses
        bin_data, labels, pc_map, addr_map, addr = bytearray(4096), {}, {}, {}, 0
        lines = source.split('\n')
        
        # Remove all comments from source and get the addresses for the labels
        # Also throw errors if incorrect formatting or arguments
        for line in lines:

            # Strip comments
            line = line.split(';')[0].strip()
            if not line: continue

            # Check for labels
            if ':' in line: 
                label, line = line.split(':', 1)
                labels[label.strip()] = addr
                line = line.strip()
            if not line: continue

            # Parse mnemonics/directives
            tokens = line.split(maxsplit=1)
            dir_mne = tokens[0].upper()
            if dir_mne == '.ASCII':
                if len(tokens) < 2 or not tokens[1].startswith('"') or not tokens[1].endswith('"') or len(tokens[1]) < 2:
                    raise ValueError('.ASCII string is malformed')
                addr += len(tokens[1][1:-1])     # push address tracker to end of string
            elif dir_mne == '.ASCIIZ':
                if len(tokens) < 2 or not tokens[1].startswith('"') or not tokens[1].endswith('"') or len(tokens[1]) < 2:
                    raise ValueError('.ASCIIZ string is malformed')
                addr += len(tokens[1][1:-1]) + 1    # push address tracker to end of string + null terminator
            elif dir_mne == '.ORG':
                if len(tokens) < 2 or ' ' in tokens[1]:
                    raise ValueError('.ORG must have one value')
                addr = int(tokens[1], 0)    # Jump to address where you set the origin
            elif dir_mne == '.SPACE':
                if len(tokens) < 2 or ' ' in tokens[1]:
                    raise ValueError('.SPACE must have one value')
                addr += int(tokens[1], 0)   # Jump forward specified number of bytes to leave them empty
            elif dir_mne == '.BYTE':
                if len(tokens) < 2:
                    raise ValueError('.BYTE must contain values')
                byte = tokens[1].replace(' ', '').split(',')    # Get byte(s)
                addr += len(byte)
            else:
                if dir_mne not in self.IMPLIED and dir_mne not in self.IMMEDIATE and dir_mne not in self.ADDRESS:
                    raise ValueError(f'"{dir_mne.upper()}" is not a defined mnemonic')
                addr += 1 if dir_mne in self.IMPLIED else 2

        # Second iteration generates the actual bytearray for the program
        addr = 0
        for i, line in enumerate(lines):

            line = line.split(';')[0].strip()
            if ':' in line: line = line.split(':', 1)[1].strip()
            if not line: continue
            
            pc_map[addr] = i
            addr_map[i] = addr 

            parts = line.split(maxsplit=1)
            dir_mne = parts[0].upper()

            # Adjust address if we encounter .org
            if dir_mne == '.ORG':
                addr = int(parts[1], 0)
            elif dir_mne == '.ASCII':
                for j, char in enumerate(parts[1][1:-1]):
                    bin_data[addr + j] = ord(char)
                addr += len(parts[1][1:-1])
            elif dir_mne == '.ASCIIZ':
                for j, char in enumerate(parts[1][1:-1]):
                    bin_data[addr + j] = ord(char)
                addr += len(parts[1][1:-1]) + 1
            elif dir_mne == '.SPACE':
                addr += int(parts[1], 0)
            elif dir_mne == '.BYTE':
                byte = parts[1].replace(' ', '').split(',')
                for j, b in enumerate(byte):
                    bin_data[addr + j] = int(b, 0)
                addr += len(byte)
            elif dir_mne in self.IMPLIED:
                bin_data[addr] = self.IMPLIED[dir_mne]
                addr += 1
            elif dir_mne in self.IMMEDIATE:
                bin_data[addr] = self.IMMEDIATE[dir_mne]
                val = int(parts[1], 0) & 0xFF
                bin_data[addr+1] = val
                addr += 2
            elif dir_mne in self.ADDRESS:
                op = self.ADDRESS[dir_mne]
                target = labels[parts[1]] if parts[1] in labels else int(parts[1], 0)
                bin_data[addr] = (op << 4) | ((target >> 8) & 0xF)
                bin_data[addr+1] = target & 0xFF
                addr += 2
        return bin_data, pc_map, addr_map
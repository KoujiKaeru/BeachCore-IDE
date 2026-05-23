from PySide6 import QtCore, QtGui, QtWidgets

# Much of this code was pulled from the PyQt wiki page
# https://wiki.python.org/moin/PyQt/Python%20syntax%20highlighting


def format(color, style=''):
    """
    Return a QTextCharFormat with the given attributes.
    """
    _color = QtGui.QColor()
    _color.setNamedColor(color)

    _format = QtGui.QTextCharFormat()
    _format.setForeground(_color)
    if 'bold' in style:
        _format.setFontWeight(QtGui.QFont.Bold)
    if 'italic' in style:
        _format.setFontItalic(True)

    return _format

STYLES_ASM = {
    'keyword': format("#c87df0"),                  # Instructions
    'label': format("#fa6464"),                       # Labels for code (for jumps)
    'comment': format('darkGray', 'italic'),      # Comments
    'numbers': format("#72cafd"),               # Numbers for addresses and immediate data
    'directive': format("#7df09e", 'italic'),
    'string': format("#f08dae")
}

class AsmHighlighter(QtGui.QSyntaxHighlighter):
    """
    Syntax highlighter for LBTiny assembly.
    """
    # Instruction keywords
    keywords = [
        'NOP', 'SHR', 'SHL', 'EI', 'DI', 'RETI', 'HALT', 'INV',
        'LDI', 'ADDI', 'ANDI', 'ORI', 'XORI', 'LD', 'ST', 'ADD', 'AND', 'OR', 'XOR', 
        'JMP', 'JZ', 'JNZ', 'JC', 'JNC', 'INC', 'DEC', 'SUB', 'SUBI'
    ]

    # Directives
    directives = [
        'org', 'space', 'byte', 'ascii', 'asciiz'
    ]

    def __init__(self, parent: QtGui.QTextDocument) -> None:
        super().__init__(parent)
        rules = []

        # Keyword rules
        rules += [(r'(?i)\b%s\b' % w, 0, STYLES_ASM['keyword']) for w in AsmHighlighter.keywords]

        # Directive rules
        rules += [(r'.(?i)\b%s\b' % w, 0, STYLES_ASM['directive']) for w in AsmHighlighter.directives]
        
        # All other rules
        rules += [
 
            # Numeric literals
            (r'\b[+-]?[0-9]+[lL]?\b', 0, STYLES_ASM['numbers']),
            (r'\b[+-]?0[xX][0-9A-Fa-f]+[lL]?\b', 0, STYLES_ASM['numbers']),
            (r'\b[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\b', 0, STYLES_ASM['numbers']),

            # From ';' until a newline
            (r';[^\n]*', 0, STYLES_ASM['comment']),

            # Before ':' is a label
            (r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', 1, STYLES_ASM['label']),

            # Strings
            (r'([\"])(.*)\1', 0, STYLES_ASM['string'])
        ]

        # Build a QRegularExpression for each pattern
        self.rules = [(QtCore.QRegularExpression(pat), index, fmt) for (pat, index, fmt) in rules]

    def highlightBlock(self, text):
         """
         Apply syntax highlighting to the given block of text.
         """
         # Do syntax formatting
         for expression, nth, text_format in self.rules:
             match = expression.match(text)
             while match.hasMatch():
                 # We actually want the index of the nth match
                 index = match.capturedStart(nth)
                 length = match.capturedLength(nth)
                 if index >= 0:
                     self.setFormat(index, length, text_format)
                 start = index + length
                 match = expression.match(text, start)

         self.setCurrentBlockState(0)
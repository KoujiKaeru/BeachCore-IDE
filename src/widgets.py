# Widgets used by main.py to generate the GUI
from PySide6.QtCore import Qt, QRect, QSize, QObject, QEvent, Signal, QPoint
from PySide6.QtGui import (QColor, QFont, QPainter, QPen, QPolygon)
from PySide6.QtWidgets import (QPlainTextEdit, 
                               QWidget)
import sys
import os

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        # Get the absolute path of the directory containing this script (the 'src' folder)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # Go up one level to the project root
        base_path = os.path.abspath(os.path.join(script_dir, '..'))

    return os.path.join(base_path, relative_path)

# Corner Button Event Filter
class CornerPainter(QObject):
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Paint:
            painter = QPainter(obj)
            painter.fillRect(obj.rect(), QColor(49, 54, 59)) 
            painter.setPen(QColor(252, 252, 252))
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(obj.rect(), Qt.AlignCenter, "Addr")
            painter.end()
            return True 
        return False

# Line Number Widget
class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.codeEditor = editor

    def sizeHint(self):
        return QSize(self.codeEditor.lineNumberAreaWidth(), 0)

    def paintEvent(self, event):
        self.codeEditor.lineNumberAreaPaintEvent(event)

    def mousePressEvent(self, event):
        # Pass the raw click coordinates back to the parent editor
        self.codeEditor.handle_gutter_click(event.position().toPoint())

class CodeEditor(QPlainTextEdit):
    breakpoint_toggled = Signal(int)

    def __init__(self):
        super().__init__()

        win_font = QFont("Courier New", 11)  # more reliable than "Monospace" on Windows
        win_font.setStyleHint(QFont.Monospace)  # ← fallback hint if Courier New isn't found
        win_font.setFixedPitch(True)           # ← forces fixed-pitch selection
        self.setFont(QFont("Monospace", 11)) # Default "Monospace" unless in windows
        if os.name == 'nt':
            self.setFont(win_font)
        self.lineNumberArea = LineNumberArea(self)

        self.blockCountChanged.connect(self.updateLineNumberAreaWidth)
        self.updateRequest.connect(self.updateLineNumberArea)
        self.updateLineNumberAreaWidth(0)

        # ADD THIS — force geometry immediately after widget is shown
        self.updateGeometry()

    def handle_gutter_click(self, pos):
        # We still only care about the Y coordinate to find the line!
        cursor = self.cursorForPosition(QPoint(0, pos.y()))
        block = cursor.block()
        
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        
        if top <= pos.y() <= bottom:
            self.breakpoint_toggled.emit(block.blockNumber())
    
    def lineNumberAreaWidth(self):
        digits = 1
        max_v = max(1, self.blockCount())
        while max_v >= 10:
            max_v /= 10
            digits += 1
        # increase width between start of characters and line number gutter
        space = 40 + self.fontMetrics().horizontalAdvance('9') * digits  # was 35
        return space

    def updateLineNumberAreaWidth(self, _):
        self.setViewportMargins(self.lineNumberAreaWidth(), 0, 0, 0)

    def updateLineNumberArea(self, rect, dy):
        if dy:
            self.lineNumberArea.scroll(0, dy)
        else:
            self.lineNumberArea.update(0, rect.y(), self.lineNumberArea.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.updateLineNumberAreaWidth(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        w = self.lineNumberAreaWidth()
        self.lineNumberArea.setGeometry(QRect(cr.left(), cr.top(), w, cr.height()))
        self.setViewportMargins(w, 0, 0, 0) #without this, first letter gets cut


    def lineNumberAreaPaintEvent(self, event):
        painter = QPainter(self.lineNumberArea)
        painter.fillRect(event.rect(), QColor("#222"))

        main_win = self.window()
        is_stale = getattr(main_win, "is_stale", True)
        breakpoints = getattr(main_win, "breakpoints", set())
        addr_map = getattr(main_win, "addr_map", {})
        pc_map = getattr(main_win, "pc_map", {})
        
        active_line = pc_map.get(main_win.cpu.pc, -1) if hasattr(main_win, "cpu") else -1

        block = self.firstVisibleBlock()
        blockNumber = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                
                addr = addr_map.get(blockNumber)
                is_executable = addr is not None
                has_bp = addr in breakpoints if is_executable else False
                is_active = (blockNumber == active_line)
                
                line_height = self.fontMetrics().height()
                r = line_height - 4

                # --- 1. Draw Breakpoint Dot ---
                if has_bp:
                    painter.setRenderHint(QPainter.Antialiasing)
                    # If stale, we use a faded red (Ghost Breakpoint)
                    bp_color = QColor(200, 40, 40, 100 if is_stale else 255)
                    painter.setBrush(bp_color)
                    painter.setPen(Qt.NoPen)
                    painter.drawEllipse(5, top + 2, r, r)

                # --- 2. Draw Execution Arrow ---
                if is_active:
                    painter.setRenderHint(QPainter.Antialiasing)
                    # If stale, the arrow turns gray to show it's untrusted
                    arrow_color = QColor(150, 150, 150) if is_stale else QColor(250, 200, 50)
                    pen = QPen(arrow_color)
                    pen.setWidth(2)
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    
                    arrow_x, arrow_y = 5, top + 2
                    poly = QPolygon([
                        QPoint(arrow_x + 1, arrow_y + 1),
                        QPoint(arrow_x + r - 3, arrow_y + r // 2),
                        QPoint(arrow_x + 1, arrow_y + r - 1)
                    ])
                    painter.drawPolygon(poly)

                # --- 3. Draw Line Number ---
                # Executable lines = Bright White/Gold
                # Non-executable lines = Dark Gray
                if is_executable:
                    num_color = QColor("#FFF") if not is_stale else QColor("#AAA")
                else:
                    num_color = QColor("#444") # Very dim

                painter.setPen(num_color)
                #-10 pushes numbers to the left in the gutter
                painter.drawText(0, top, self.lineNumberArea.width() - 10, line_height,
                                 Qt.AlignRight, str(blockNumber + 1))

            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            blockNumber += 1
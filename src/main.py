import sys
import os
from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtGui import (QColor, QFont, QAction, QTextFormat, QTextCursor, 
                           QPalette, QIcon)
from PySide6.QtWidgets import (QApplication, QMainWindow,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, 
                               QHBoxLayout, QWidget, QSplitter, 
                               QHeaderView, QMessageBox, QLabel, QTextEdit,
                               QFileDialog, QAbstractButton, QLineEdit, 
                               QPushButton, QDialog, QProgressBar)
from core import CPU, Assembler
from hw_transfer import HwTransferPanel
from syntax import *
from widgets import *

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cpu, self.assembler, self.pc_map, self.addr_map = CPU(), Assembler(), {}, {}
        self.breakpoints = set()
        self.current_file = None
        self.is_stale = True # Start stale so user MUST assemble first  
        
        self.settings = QSettings("LBTiny-IDE")
        saved_watches = self.settings.value("watched_bases", [])
        self.watched_bases = [int(w) for w in saved_watches] if saved_watches else []
        
        #disable saving and loading breakpoints
        #saved_bps = self.settings.value("breakpoints", [])
        #self.breakpoints = {int(b, 16) for b in saved_bps} if saved_bps else set()

        self.timer = QTimer(); self.timer.timeout.connect(self.do_timer_step)
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("LBTiny-IDE"); self.resize(1200, 800)

        # Safely set the window icon
        icon_path = resource_path(os.path.join("resources", "icon.png"))
        self.setWindowIcon(QIcon(icon_path))
        
        self.editor = CodeEditor()
        # Wire the new signal directly to the toggle function
        self.editor.breakpoint_toggled.connect(self.toggle_breakpoint)
        
        self.reg_view = QTableWidget(9, 2)
        self.reg_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.reg_view.verticalHeader().hide()
        self.reg_view.horizontalHeader().hide()

        header_labels = [f"{i:02X}" for i in range(16)]

        self.watch_view = QTableWidget(0, 16)
        self.watch_view.horizontalHeader().setMinimumSectionSize(10)
        self.watch_view.horizontalHeader().setDefaultSectionSize(25)
        self.watch_view.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.watch_view.verticalHeader().setFixedWidth(40)
        self.watch_view.setHorizontalHeaderLabels(header_labels)
        
        self.watch_corner_painter = CornerPainter(self)
        watch_corner = self.watch_view.findChild(QAbstractButton)
        if watch_corner:
            watch_corner.installEventFilter(self.watch_corner_painter)

        self.mem_view = QTableWidget(16, 16)
        self.mem_view.horizontalHeader().setMinimumSectionSize(10)
        self.mem_view.horizontalHeader().setDefaultSectionSize(25)
        self.mem_view.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.mem_view.verticalHeader().setFixedWidth(40)
        self.mem_view.setHorizontalHeaderLabels(header_labels)

        self.mem_corner_painter = CornerPainter(self)
        mem_corner = self.mem_view.findChild(QAbstractButton)
        if mem_corner:
            mem_corner.installEventFilter(self.mem_corner_painter)

        central = QWidget()
        layout = QHBoxLayout(central)
        
        main_splitter = QSplitter(Qt.Horizontal)
        right_splitter = QSplitter(Qt.Vertical)

        reg_widget = QWidget(); reg_layout = QVBoxLayout(reg_widget)
        reg_layout.setContentsMargins(0, 0, 0, 0)
        reg_layout.addWidget(QLabel("<b>Registers</b>")); reg_layout.addWidget(self.reg_view)
        right_splitter.addWidget(reg_widget)

        watch_widget = QWidget(); watch_layout = QVBoxLayout(watch_widget)
        watch_layout.setContentsMargins(0, 0, 0, 0)
        watch_layout.addWidget(QLabel("<b>Watch Memory</b>"))
        
        watch_controls = QHBoxLayout()
        self.watch_input = QLineEdit()
        self.watch_input.setPlaceholderText("e.g. 0C0, 0F0-0FF")
        self.watch_input.returnPressed.connect(self.do_add_watch)
        
        btn_add = QPushButton("Add")
        btn_add.clicked.connect(self.do_add_watch)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.do_clear_watch)
        
        watch_controls.addWidget(self.watch_input)
        watch_controls.addWidget(btn_add)
        watch_controls.addWidget(btn_clear)
        
        watch_layout.addLayout(watch_controls)
        watch_layout.addWidget(self.watch_view)
        right_splitter.addWidget(watch_widget)

        mem_widget = QWidget()
        mem_layout = QVBoxLayout(mem_widget)
        mem_layout.setContentsMargins(0, 0, 0, 0)
        
        # Add the title and the main memory text area
        mem_layout.addWidget(QLabel("<b>Program Memory</b>"))
        mem_layout.addWidget(self.mem_view)
        
        # --- NEW: Memory Status Widgets ---
        self.mem_widget_label = QLabel("Memory Usage: 0/3072 bytes")
        
        self.mem_progress_bar = QProgressBar()
        self.mem_progress_bar.setTextVisible(False)
        self.mem_progress_bar.setFixedHeight(10)
        self.mem_progress_bar.setMaximum(3072) # 3KB limit

        # Create a horizontal layout to hold the label and progress bar side-by-side
        status_layout = QHBoxLayout()
        status_layout.addWidget(self.mem_widget_label)   
        #status_layout.addWidget(self.mem_progress_bar)
        status_layout.addWidget(self.mem_progress_bar, 1)   
        #status_layout.addStretch()

        # Add this horizontal strip to the bottom of the vertical memory layout
        mem_layout.addLayout(status_layout)
        
        # Finally, add the whole memory widget block to the right side of your app
        right_splitter.addWidget(mem_widget)

        right_splitter.setSizes([200, 300, 300])
        main_splitter.addWidget(self.editor)
        main_splitter.addWidget(right_splitter)

        # Hardware transfer panel — docks to the right of the register/watch/memory
        # column instead of opening as a floating dialog. Hidden until the user
        # clicks the "Download…" toolbar action.
        self.hw_panel = HwTransferPanel(self)
        self.hw_panel.hide()
        main_splitter.addWidget(self.hw_panel)

        main_splitter.setSizes([700, 500, 380])

        layout.addWidget(main_splitter)
        self.setCentralWidget(central)

        # FILE MENU------------------------------------------
        file_menu = self.menuBar().addMenu("&File")
        file_actions = [
            ("New", "Ctrl+N", self.do_new),
            ("Open", "Ctrl+O", self.do_open),
            ("Save", "Ctrl+S", self.do_save),
            ("Save As", "Ctrl+Shift+S", self.do_save_as)
        ]
        for text, shortcut, slot in file_actions:
            act = QAction(text, self)
            act.setShortcut(shortcut)
            act.triggered.connect(slot)
            file_menu.addAction(act)
        
        sim_menu = self.menuBar().addMenu("&Simulation")
        speeds = [
            ("1.0 s/instr (Crawl)", 1000),
            ("0.5 s/instr (Slow)", 500),
            ("0.05 s/instr (Default)", 50),
            ("0.01 s/instr (Fast)", 10),
            ("0.001 s/instr (Ultra)", 1)
        ]
        self.speed_actions = []
        for text, ms in speeds:
            act = QAction(text, self, checkable=True)
            act.triggered.connect(lambda checked, m=ms, a=act: self.set_sim_speed(m, a))
            sim_menu.addAction(act)
            self.speed_actions.append(act)
            if ms == 50:
                act.setChecked(True)
                self.current_interval = 50

        self.sample_menu = self.menuBar().addMenu("&Sample Code")
        self.populate_sample_menu()
        
        # help menu
        help_menu = self.menuBar().addMenu("&Help")

        # help -> ISR reference
        instr_act = QAction("Instruction Set &Reference", self)
        instr_act.triggered.connect(self.show_instructions)
        help_menu.addAction(instr_act)

        # help -> Directives Reference
        dir_act = QAction("ASM &Directives Reference", self)
        dir_act.triggered.connect(self.show_directives)
        help_menu.addAction(dir_act)

        # help -> about
        about_act = QAction("&About LBTiny-IDE", self)
        about_act.triggered.connect(self.show_about)
        help_menu.addAction(about_act)

        # TOOLBAR--------------------------------------------
        t = self.addToolBar("Controls")
        control_actions = [
            ("Assemble (F7)", "F7", self.do_assemble),
            ("Step (F10)", "F10", self.do_step),
            ("Run (F5)", "F5", self.do_run),
            ("Stop (Esc)", "Esc", self.timer.stop),
            ("Reset (Ctrl+R)", "Ctrl+R", self.do_reset)
        ]
        for text, shortcut, slot in control_actions:
            act = QAction(text, self)
            act.setShortcut(shortcut)
            act.triggered.connect(slot)
            t.addAction(act)
        
        t.addAction(QAction("INT Trigger", self, triggered=self.cpu.trigger_interrupt))

        # Hardware group - separated from simulation controls
        t.addSeparator()
        download_act = QAction("Program…", self)
        download_act.setCheckable(True)
        download_act.setToolTip("Toggle LBTiny Hardware Transfer panel")
        download_act.triggered.connect(self.show_download_dialog)
        t.addAction(download_act)

        self.setup_watch_grid()

        geometry = self.settings.value("geometry")
        if geometry: self.restoreGeometry(geometry)
        main_state = self.settings.value("main_splitter_state")
        if main_state: main_splitter.restoreState(main_state)
        right_state = self.settings.value("right_splitter_state")
        if right_state: right_splitter.restoreState(right_state)

        self.main_splitter = main_splitter
        self.right_splitter = right_splitter

        self.editor.textChanged.connect(self.mark_stale)

        self.update_ui()

    def show_download_dialog(self):
        """
        Toggle the embedded hardware transfer panel. The panel docks as a
        column inside the main splitter so it doesn't cover the editor /
        register / memory views.
        """
        if self.hw_panel.isVisible():
            self.hw_panel.hide()
            self._hw_action.setChecked(False)
        else:
            self.hw_panel.refresh_binary_info()
            self.hw_panel.show()
            self._hw_action.setChecked(True)
            # If the splitter has collapsed the panel column to zero width
            # (e.g. from a saved state), give it a sensible default again.
            sizes = self.main_splitter.sizes()
            if len(sizes) >= 3 and sizes[-1] < 50:
                total = sum(sizes)
                panel_w = max(380, total // 4)
                # Steal proportionally from the other two columns.
                remaining = max(total - panel_w, 200)
                left = int(remaining * sizes[0] / max(sizes[0] + sizes[1], 1))
                right = remaining - left
                self.main_splitter.setSizes([left, right, panel_w])

    def populate_sample_menu(self):
        self.sample_menu.clear()
        samples_path = resource_path(os.path.join("resources", "samplecode"))
        
        try:
            files = sorted(f for f in os.listdir(samples_path) if f.endswith('.asm'))
            if not files:
                act = QAction("No samples found", self)
                act.setEnabled(False)
                self.sample_menu.addAction(act)
                return
            for filename in files:
                full_path = os.path.join(samples_path, filename)
                act = QAction(filename, self)
                act.triggered.connect(lambda checked, p=full_path: self.load_sample(p))
                self.sample_menu.addAction(act)
        except FileNotFoundError:
            act = QAction("Sample folder not found", self)
            act.setEnabled(False)
            self.sample_menu.addAction(act)

    def load_sample(self, path):
        if self.editor.toPlainText().strip():
            reply = QMessageBox.question(self, "Load Sample",
                "This will replace your current code. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                return
        try:
            with open(path, 'r') as f:
                content = f.read()
            self.editor.blockSignals(True)
            self.editor.setPlainText(content)
            self.editor.blockSignals(False)
            self.current_file = None
            self.setWindowTitle(f"LBTiny-IDE - {os.path.basename(path)} (Sample)")
            self.do_assemble()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not load sample: {e}")

    def mark_stale(self):
        """Called whenever the user types in the editor."""
        if not self.is_stale:
            self.is_stale = True
            self.statusBar().showMessage("Source changed - Assembly required", 0)
            self.update_ui() # To refresh button states/colors

    def show_instructions(self):
        # Store the dialog as an attribute of MainWindow (self.instr_dialog)
        # This keeps it alive in memory while you interact with the editor.
        if not hasattr(self, 'instr_dialog') or self.instr_dialog is None:
            self.instr_dialog = InstructionSetDialog(self)
        
        # Modeless windows use .show() instead of .exec()
        self.instr_dialog.show()
        self.instr_dialog.raise_() # Bring to front if already open
        self.instr_dialog.activateWindow()

    def show_directives(self):
        # Store the dialog as an attribute of MainWindow (self.instr_dialog)
        # This keeps it alive in memory while you interact with the editor.
        if not hasattr(self, 'dir_dialog') or self.dir_dialog is None:
            self.dir_dialog = DirectivesDialog(self)
        
        # Modeless windows use .show() instead of .exec()
        self.dir_dialog.show()
        self.dir_dialog.raise_() # Bring to front if already open
        self.dir_dialog.activateWindow()

    def show_about(self):
        dialog = AboutDialog(self)
        dialog.exec()

    def set_sim_speed(self, ms, action):
        for act in self.speed_actions: act.setChecked(False)
        action.setChecked(True)
        self.current_interval = ms
        if self.timer.isActive(): self.timer.start(self.current_interval)

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("main_splitter_state", self.main_splitter.saveState())
        self.settings.setValue("right_splitter_state", self.right_splitter.saveState())
        #disable saving and loading breakpoints
        #self.settings.setValue("breakpoints", [f"{b:03X}" for b in self.breakpoints])

        # Stop the hardware-transfer worker thread cleanly.
        if hasattr(self, "hw_panel"):
            try:
                self.hw_panel.shutdown()
            except Exception:
                pass

        super().closeEvent(event)        

    # --- BREAKPOINT LOGIC ---
    def toggle_breakpoint(self, line_number):
        # If the user clicks a comment, look ahead for the next instruction
        target_line = line_number
        while target_line < self.editor.blockCount() and target_line not in self.addr_map:
            target_line += 1
            
        if target_line in self.addr_map:
            addr = self.addr_map[target_line]
            if addr in self.breakpoints:
                self.breakpoints.remove(addr)
            else:
                self.breakpoints.add(addr)
            self.editor.lineNumberArea.update()
        else:
            self.statusBar().showMessage("No executable code found from this line downward.", 3000)

    # --- WATCH WINDOW LOGIC ---
    def do_add_watch(self):
        text = self.watch_input.text().strip()
        if not text: return
        new_bases = set(self.watched_bases)
        try:
            tokens = [t.strip() for t in text.split(',')]
            for token in tokens:
                if '-' in token:
                    start_str, end_str = token.split('-')
                    start, end = int(start_str, 16) & 0xFF0, int(end_str, 16) & 0xFF0
                    if start > end: start, end = end, start
                    for addr in range(start, end + 16, 16): new_bases.add(addr)
                else:
                    new_bases.add(int(token, 16) & 0xFF0)
            self.watched_bases = sorted(list(new_bases))
            self.settings.setValue("watched_bases", [str(b) for b in self.watched_bases])
            self.watch_input.clear()
            self.setup_watch_grid()
            self.update_ui()
        except ValueError:
            QMessageBox.warning(self, "Invalid Input", "Please use hex values (e.g., 0C0 or C00-C20).")

    def do_clear_watch(self):
        self.watched_bases.clear()
        self.settings.setValue("watched_bases", [])
        self.setup_watch_grid()

    def setup_watch_grid(self):
        self.watch_view.setRowCount(len(self.watched_bases))
        v_headers = [f"{b:03X}" for b in self.watched_bases]
        self.watch_view.setVerticalHeaderLabels(v_headers)

    def do_run(self):
        # Prevent running stale code
        if self.is_stale:
            self.do_assemble()
            if self.is_stale: return # Stop if assembly failed
            
        if self.cpu.pc in self.breakpoints:
            self.cpu.skip_breakpoint = True
        self.timer.start(self.current_interval)

    def do_new(self):
        if self.editor.toPlainText().strip():
            reply = QMessageBox.question(self, "Confirm New File", "You have changes in the editor. Are you sure you want to clear it?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No: return
        
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        
        self.current_file = None
        self.setWindowTitle("LBTiny-IDE - New File")
        self.pc_map = {}; self.addr_map = {}
        self.is_stale = True # Blank files need assembly
        self.update_ui()

    def do_open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open ASM File", "", "Assembly Files (*.asm);;All Files (*)")
        if path:
            try:
                with open(path, 'r') as f: 
                    file_content = f.read()
                
                # Block signals so loading text doesn't trigger "mark_stale"
                self.editor.blockSignals(True)
                self.editor.setPlainText(file_content)
                self.editor.blockSignals(False)
                
                self.current_file = path
                self.setWindowTitle(f"LBTiny-IDE - {os.path.basename(path)}")
                
                # Auto-assemble the file you just opened!
                self.do_assemble() 
            except Exception as e: 
                QMessageBox.critical(self, "Error", f"Could not open file: {e}")

    def do_save(self):
        if self.current_file:
            try:
                with open(self.current_file, 'w') as f: f.write(self.editor.toPlainText())
            except Exception as e: QMessageBox.critical(self, "Error", f"Could not save file: {e}")
        else: self.do_save_as()

    def do_save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save ASM File", "", "Assembly Files (*.asm);;All Files (*)")
        if path:
            if not path.endswith('.asm'): path += '.asm'
            self.current_file = path
            self.do_save()
            self.setWindowTitle(f"LBTiny-IDE - {os.path.basename(path)}")

    def do_assemble(self):
        try:
            self.cpu.mem, self.pc_map, self.addr_map = self.assembler.assemble(self.editor.toPlainText())
            
            if self.addr_map:
                max_address = max(self.addr_map.values())
                # Real assembled bytes, not a zero-filled mock. Trim to the last
                # instruction (max_address points at the start of the final opcode, which
                # may be 1 or 2 bytes long; +2 is a safe upper bound).
                end = min(max_address + 2, len(self.cpu.mem))
                self.current_binary = bytes(self.cpu.mem[:end])
            else:
                self.current_binary = []
            
            # Clear Stale State
            self.cpu.reset()
            self.breakpoints.clear()
            self.is_stale = False
            self.statusBar().showMessage("Assembly Successful. CPU Ready.", 3000)
            self.update_ui()

            # Push fresh binary metadata into the hw panel if it's open.
            if hasattr(self, "hw_panel"):
                self.hw_panel.refresh_binary_info()

        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def do_timer_step(self):
        # --- FIX: Stop the runaway train if user edits code mid-run ---
        if self.is_stale:
            self.timer.stop()
            self.statusBar().showMessage("Simulation stopped: Code was edited during run.", 3000)
            self.update_ui()
            return
        # --------------------------------------------------------------

        # This is strictly for the auto-run timer.
        if self.cpu.pc in self.breakpoints and not self.cpu.skip_breakpoint:
            self.timer.stop()
            self.statusBar().showMessage(f"Breakpoint hit at 0x{self.cpu.pc:03X}", 3000)
            self.update_ui()
            return

        self.cpu.step()
        self.cpu.skip_breakpoint = False 
        self.update_ui()

    def do_step(self):
        # Prevent stepping on stale code
        if self.is_stale:
            self.do_assemble()
            if self.is_stale: return # Abort if assembly failed

        self.cpu.step()
        self.update_ui()

    def do_reset(self):
        self.cpu.reset()
        self.statusBar().clearMessage()
        self.update_ui()

    def update_ui(self):
        # Flash the "Program Memory" label if stale
        stale_style = "color: #FFA500; font-weight: bold;" if self.is_stale else "color: white; font-weight: bold;"
        if hasattr(self, 'mem_label'):
            self.mem_label.setStyleSheet(stale_style)
            self.mem_label.setText("Program Memory (STALE)" if self.is_stale else "Program Memory")

        # Visual indicator for memory usage
        current_size = len(self.current_binary) if hasattr(self, 'current_binary') and self.current_binary else 0
        self.mem_widget_label.setText(f"Memory Usage: {current_size}/3072 bytes")
        self.mem_progress_bar.setValue(current_size)

        # Visual indicator for stale memory
        if self.is_stale:
            mem_label_color = "#FFA500"
        elif current_size > 3072:
            mem_label_color = "red"
        else:
            mem_label_color = "green"
        self.mem_widget_label.setStyleSheet(f"color: {mem_label_color}; font-weight: bold;")

        regs = [
            ("PC", f"{self.cpu.pc:03X}"), ("ACC", f"{self.cpu.acc:02X}"),
            ("C", self.cpu.c), ("Z", self.cpu.z), ("IE", self.cpu.ie),
            ("ISR", self.cpu.in_isr), ("PC_SAVE", f"{self.cpu.pc_save:03X}"),
            ("Cycles", self.cpu.cycles), ("HALTED", "YES" if self.cpu.halted else "NO")
        ]
        self.reg_view.setRowCount(len(regs))
        for i, (n, v) in enumerate(regs):
            self.reg_view.setItem(i, 0, QTableWidgetItem(n))
            self.reg_view.setItem(i, 1, QTableWidgetItem(str(v)))

        line = self.pc_map.get(self.cpu.pc)
        if line is not None and not self.is_stale:  # ← add the stale check
            sel = QTextEdit.ExtraSelection()
            if self.cpu.pc in self.breakpoints and not self.timer.isActive():
                sel.format.setBackground(QColor(180, 150, 0))
            else:
                sel.format.setBackground(self.palette().highlight().color())
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = QTextCursor(self.editor.document().findBlockByLineNumber(line))
            self.editor.setExtraSelections([sel])
        else:
            self.editor.setExtraSelections([])  # ← clear it when stale

        base = (self.cpu.pc >> 4) << 4
        v_headers = [f"{base+(r*16):03X}" for r in range(16)]
        self.mem_view.setVerticalHeaderLabels(v_headers)

        for r in range(16):
            for c in range(16):
                addr = base+(r*16)+c
                it = QTableWidgetItem(f"{self.cpu.mem[addr]:02X}")
                if addr == self.cpu.pc: it.setBackground(self.palette().highlight().color())
                self.mem_view.setItem(r, c, it)

        for r, watch_base in enumerate(self.watched_bases):
            for c in range(16):
                addr = watch_base + c
                if addr < 4096:
                    it = QTableWidgetItem(f"{self.cpu.mem[addr]:02X}")
                    if addr == self.cpu.pc: it.setBackground(self.palette().highlight().color())
                    self.watch_view.setItem(r, c, it)

        # Forces the gutter to repaint the arrow at the new PC location
        self.editor.lineNumberArea.update()

class InstructionSetDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        # This flag keeps the window floating on top of the main IDE
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Instruction Set Reference")
        self.setFixedSize(700, 900)
        
        layout = QVBoxLayout(self)
        
        #self.text_display = QTextEdit()
        #self.text_display.setReadOnly(True)
        #self.text_display.setFont(QFont("Monospace", 10))

        self.text_display = QTextEdit()
        self.text_display.setReadOnly(True)
        font = QFont("Courier New", 10)  # more reliable than "Monospace" on Windows
        font.setStyleHint(QFont.Monospace)  # ← fallback hint if Courier New isn't found
        font.setFixedPitch(True)           # ← forces fixed-pitch selection
        self.text_display.setFont(font)
        
        # Load the text from the resources folder
        instr_path = resource_path(os.path.join("resources", "isr.txt"))
        try:
            with open(instr_path, 'r') as f:
                self.text_display.setPlainText(f.read())
        except Exception as e:
            self.text_display.setPlainText(f"Error loading instructions: {e}")
            
        layout.addWidget(self.text_display)
        
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignRight)

class DirectivesDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        # This flag keeps the window floating on top of the main IDE
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("ASM Directives Reference")
        self.setFixedSize(750, 600)
        
        layout = QVBoxLayout(self)
        
        #self.text_display = QTextEdit()
        #self.text_display.setReadOnly(True)
        #self.text_display.setFont(QFont("Monospace", 10))

        self.text_display = QTextEdit()
        self.text_display.setReadOnly(True)
        font = QFont("Courier New", 10)  # more reliable than "Monospace" on Windows
        font.setStyleHint(QFont.Monospace)  # ← fallback hint if Courier New isn't found
        font.setFixedPitch(True)           # ← forces fixed-pitch selection
        self.text_display.setFont(font)
        
        # Load the text from the resources folder
        instr_path = resource_path(os.path.join("resources", "directives.txt"))
        try:
            with open(instr_path, 'r') as f:
                self.text_display.setPlainText(f.read())
        except Exception as e:
            self.text_display.setPlainText(f"Error loading directives: {e}")
            
        layout.addWidget(self.text_display)
        
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignRight)

class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About LBTiny-IDE")
        self.setFixedSize(400, 350)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(30, 30, 30, 30)

        # 1. The Icon (Prominent)
        icon_label = QLabel()
        icon_path = resource_path(os.path.join("resources", "icon.png"))
        pixmap = QIcon(icon_path).pixmap(128, 128) # Large 128x128 display
        icon_label.setPixmap(pixmap)
        icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_label)

        # 2. The Text
        title = QLabel("Project LBTiny-IDE")
        title.setStyleSheet("font-size: 18pt; font-weight: bold; color: #B49600;") # Arcana Gold
        title.setAlignment(Qt.AlignCenter)
        
        subtitle = QLabel("Minimalistic 8-bit CPU - Designed at CSULB")
        subtitle.setStyleSheet("font-size: 12pt; margin-bottom: 10px;")
        subtitle.setAlignment(Qt.AlignCenter)

        credits = QLabel(
            "By: William Dessert & Sarah Gooneratne\n"
            "Advisor: Eric Hernandez"
        )
        credits.setAlignment(Qt.AlignCenter)
        credits.setStyleSheet("color: #AAA; line-height: 150%;")

        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addStretch() # Space between title and credits
        layout.addWidget(credits)
        layout.addStretch()

        # 3. Close Button
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close, alignment=Qt.AlignCenter)

if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion")

    arcana_palette = QPalette()
    arcana_palette.setColor(QPalette.Window, QColor(42, 46, 50))
    arcana_palette.setColor(QPalette.WindowText, QColor(252, 252, 252))
    arcana_palette.setColor(QPalette.Base, QColor(27, 30, 32))
    arcana_palette.setColor(QPalette.AlternateBase, QColor(35, 38, 41))
    arcana_palette.setColor(QPalette.ToolTipBase, QColor(49, 54, 59))
    arcana_palette.setColor(QPalette.ToolTipText, QColor(252, 252, 252))
    arcana_palette.setColor(QPalette.Text, QColor(252, 252, 252))
    arcana_palette.setColor(QPalette.Button, QColor(49, 54, 59))
    arcana_palette.setColor(QPalette.ButtonText, QColor(252, 252, 252))
    arcana_palette.setColor(QPalette.BrightText, QColor(75, 75, 75))
    arcana_palette.setColor(QPalette.Link, QColor(209, 199, 242))
    #arcana_palette.setColor(QPalette.Highlight, QColor(110, 86, 169)) # this is arcana purple
    arcana_palette.setColor(QPalette.Highlight, QColor(180, 150, 0)) # Now Arcana Gold 
    arcana_palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(arcana_palette)
    
    win = MainWindow()
    highlight = AsmHighlighter(win.editor.document())
    win.show()
    
    sys.exit(app.exec())


#always put the executing line back at the beginning when changing code 
#add search in code editor wth shortcut ctrl-f

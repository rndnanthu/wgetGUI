#!/usr/bin/env python3
"""
open_dir_downloader.py

A PyQt5 GUI front-end for wget specialized for downloading open directory listings.
- Builds a wget command from many options
- Runs wget as a subprocess (QProcess) and streams output to a log widget
- Supports start/stop, resume, command edit, progress parsing, presets

Requirements:
    pip install PyQt5
    system wget (Linux/Mac: usually present; Windows: install or use WSL or GNUWin32 wget)

Usage:
    python open_dir_downloader.py
"""

import sys
import shutil
import os
import json
import re
import subprocess
import urllib.request
import urllib.parse
from html.parser import HTMLParser
from PyQt5 import QtCore, QtGui, QtWidgets

APP_NAME = "OpenDir Downloader"
PRESETS_FILE = os.path.expanduser("~/.opendir_downloader_presets.json")


class DirectoryParser(HTMLParser):
    """
    Simple HTML parser to extract file links from directory listings.
    """
    def __init__(self, base_url):
        super().__init__()
        self.base_url = base_url
        self.links = []
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            for attr, value in attrs:
                if attr == 'href':
                    # Skip parent directory links, current directory, anchors, and query params
                    if value and not value.startswith('#') and not value.startswith('?'):
                        # Filter out parent (..) and current (.) directory references
                        if value not in ['../', '../', './', '.']:
                            # Resolve relative URLs
                            full_url = urllib.parse.urljoin(self.base_url, value)
                            self.links.append(full_url)
                    break


class SearchResultsDialog(QtWidgets.QDialog):
    """
    Dialog to display search results with checkboxes for selection.
    """
    def __init__(self, results, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Search Results")
        self.resize(800, 600)
        self.results = results
        self.selected_urls = []
        
        layout = QtWidgets.QVBoxLayout(self)
        
        # Info label
        info_label = QtWidgets.QLabel(f"Found {len(results)} matching files:")
        layout.addWidget(info_label)
        
        # Select all checkbox
        self.select_all_cb = QtWidgets.QCheckBox("Select All")
        self.select_all_cb.stateChanged.connect(self.toggle_select_all)
        layout.addWidget(self.select_all_cb)
        
        # List widget with checkboxes
        self.list_widget = QtWidgets.QListWidget()
        for url in results:
            item = QtWidgets.QListWidgetItem(url)
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Unchecked)
            self.list_widget.addItem(item)
        layout.addWidget(self.list_widget)
        
        # Buttons
        button_layout = QtWidgets.QHBoxLayout()
        self.continue_btn = QtWidgets.QPushButton("Continue")
        self.continue_btn.clicked.connect(self.accept)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.continue_btn)
        button_layout.addWidget(self.close_btn)
        layout.addLayout(button_layout)
    
    def toggle_select_all(self, state):
        check_state = QtCore.Qt.Checked if state == QtCore.Qt.Checked else QtCore.Qt.Unchecked
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setCheckState(check_state)
    
    def get_selected_urls(self):
        selected = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == QtCore.Qt.Checked:
                selected.append(item.text())
        return selected


def human_readable_size(num_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f}PB"


class WgetRunner(QtCore.QObject):
    """
    Wrapper around QProcess to run wget and parse output for simple progress display.
    Emits signals for log lines and progress updates.
    """
    log_line = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(int, QtCore.QProcess.ExitStatus)
    progress = QtCore.pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = QtCore.QProcess(self)
        self.process.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.on_ready)
        self.process.finished.connect(self.on_finished)

        # regex to match wget progress lines like:
        #  12% [=======>                            ]  1,234,567  23.0KB/s  eta 00:12
        self.progress_pct_re = re.compile(r"(\d{1,3})%")
        # wget also prints lines like: "Saved 'filename' ..." or "Length: 12345 (12K) [text/html]"
        self.bytes_re = re.compile(r"Length:\s*([\d,]+)")
        self.speed_eta_re = re.compile(r"([\d.,]+[KMG]?/s).+?eta\s+(\d{2}:\d{2}|\d{2}:\d{2}:\d{2})", re.I)

    def start(self, argv, working_dir=None):
        if self.process.state() != QtCore.QProcess.NotRunning:
            self.log_line.emit("Process already running.")
            return
        cmd = argv[0]
        args = argv[1:]
        self.log_line.emit(f"Starting: {cmd} {' '.join(args)}")
        if working_dir:
            self.process.setWorkingDirectory(working_dir)
        self.process.start(cmd, args)

    def stop(self):
        if self.process.state() != QtCore.QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(3000)
            self.log_line.emit("Process killed by user.")

    def on_ready(self):
        data = bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        # split into lines for emission
        for line in data.splitlines():
            self.log_line.emit(line)
            # try simple progress parsing
            m = self.progress_pct_re.search(line)
            if m:
                try:
                    pct = int(m.group(1))
                    # speed and ETA
                    speed = None
                    eta = None
                    m2 = self.speed_eta_re.search(line)
                    if m2:
                        speed = m2.group(1)
                        eta = m2.group(2)
                    self.progress.emit({"percent": pct, "speed": speed, "eta": eta, "raw": line})
                except Exception:
                    pass

    def on_finished(self, exitCode, exitStatus):
        self.log_line.emit(f"Process finished: exitCode={exitCode} exitStatus={exitStatus}")
        self.finished.emit(exitCode, exitStatus)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 720)

        self.runner = WgetRunner()
        self.runner.log_line.connect(self.append_log)
        self.runner.progress.connect(self.on_progress)
        self.runner.finished.connect(self.on_finished)

        # central widget
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Top form: URL list and destination
        form = QtWidgets.QFormLayout()
        
        # URL list widget with add/remove buttons
        url_layout = QtWidgets.QVBoxLayout()
        url_controls = QtWidgets.QHBoxLayout()
        self.url_input = QtWidgets.QLineEdit()
        self.url_input.setPlaceholderText("https://example.com/path/to/directory/")
        self.add_url_btn = QtWidgets.QPushButton("Add URL")
        self.add_url_btn.clicked.connect(self.add_url)
        self.remove_url_btn = QtWidgets.QPushButton("Remove Selected")
        self.remove_url_btn.clicked.connect(self.remove_url)
        url_controls.addWidget(self.url_input)
        url_controls.addWidget(self.add_url_btn)
        url_controls.addWidget(self.remove_url_btn)
        url_layout.addLayout(url_controls)
        
        self.url_list = QtWidgets.QListWidget()
        self.url_list.setMaximumHeight(100)
        url_layout.addWidget(self.url_list)
        
        # Search box
        search_layout = QtWidgets.QHBoxLayout()
        search_label = QtWidgets.QLabel("Search in all sources:")
        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("Enter search text (filename or pattern)")
        self.search_btn = QtWidgets.QPushButton("Search")
        self.search_btn.clicked.connect(self.search_sources)
        search_layout.addWidget(search_label)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_btn)
        url_layout.addLayout(search_layout)
        
        form.addRow("URL Sources:", url_layout)

        h = QtWidgets.QHBoxLayout()
        self.dest_edit = QtWidgets.QLineEdit(os.path.expanduser("~"))
        self.dest_browse = QtWidgets.QPushButton("Browse")
        self.dest_browse.clicked.connect(self.browse_dest)
        h.addWidget(self.dest_edit)
        h.addWidget(self.dest_browse)
        form.addRow("Destination folder:", h)

        layout.addLayout(form)

        # Options group (grid)
        options_group = QtWidgets.QGroupBox("wget options")
        options_layout = QtWidgets.QGridLayout()
        options_group.setLayout(options_layout)

        self.checkbox_recursive = QtWidgets.QCheckBox("Recursive (-r)")
        self.checkbox_recursive.setChecked(True)
        options_layout.addWidget(self.checkbox_recursive, 0, 0)

        self.checkbox_no_parent = QtWidgets.QCheckBox("No parent (-np)")
        self.checkbox_no_parent.setChecked(True)
        options_layout.addWidget(self.checkbox_no_parent, 0, 1)

        self.checkbox_mirror = QtWidgets.QCheckBox("Mirror (-m) — (implies -r -N -l inf -nr)")
        options_layout.addWidget(self.checkbox_mirror, 0, 2)

        options_layout.addWidget(QtWidgets.QLabel("Recursion depth (-l):"), 1, 0)
        self.spin_depth = QtWidgets.QSpinBox()
        self.spin_depth.setMinimum(0)
        self.spin_depth.setMaximum(99)
        self.spin_depth.setValue(5)
        options_layout.addWidget(self.spin_depth, 1, 1)

        options_layout.addWidget(QtWidgets.QLabel("Cut dirs (--cut-dirs):"), 1, 2)
        self.spin_cutdirs = QtWidgets.QSpinBox()
        self.spin_cutdirs.setMinimum(0)
        self.spin_cutdirs.setMaximum(99)
        self.spin_cutdirs.setValue(0)
        options_layout.addWidget(self.spin_cutdirs, 1, 3)

        self.checkbox_no_host_dir = QtWidgets.QCheckBox("Do not create host directories (-nH)")
        options_layout.addWidget(self.checkbox_no_host_dir, 2, 0)

        self.checkbox_timestamp = QtWidgets.QCheckBox("Timestamping (-N)")
        options_layout.addWidget(self.checkbox_timestamp, 2, 1)

        self.checkbox_continue = QtWidgets.QCheckBox("Continue / Resume (-c)")
        options_layout.addWidget(self.checkbox_continue, 2, 2)

        options_layout.addWidget(QtWidgets.QLabel("Rate limit (KB/s, --limit-rate):"), 3, 0)
        self.limit_rate_edit = QtWidgets.QLineEdit()
        self.limit_rate_edit.setPlaceholderText("e.g. 50k or 1m (leave blank for unlimited)")
        options_layout.addWidget(self.limit_rate_edit, 3, 1, 1, 3)

        options_layout.addWidget(QtWidgets.QLabel("Max retries (--tries):"), 4, 0)
        self.spin_retries = QtWidgets.QSpinBox()
        self.spin_retries.setMinimum(0)
        self.spin_retries.setMaximum(999)
        self.spin_retries.setValue(20)
        options_layout.addWidget(self.spin_retries, 4, 1)

        options_layout.addWidget(QtWidgets.QLabel("Timeout (--timeout sec):"), 4, 2)
        self.spin_timeout = QtWidgets.QSpinBox()
        self.spin_timeout.setMinimum(0)
        self.spin_timeout.setMaximum(3600)
        self.spin_timeout.setValue(30)
        options_layout.addWidget(self.spin_timeout, 4, 3)

        # Accept / Reject file types
        options_layout.addWidget(QtWidgets.QLabel("Accept file types (--accept, comma sep):"), 5, 0, 1, 2)
        self.accept_edit = QtWidgets.QLineEdit()
        self.accept_edit.setPlaceholderText("e.g. jpg,png,zip,tar.gz")
        options_layout.addWidget(self.accept_edit, 5, 2, 1, 2)

        options_layout.addWidget(QtWidgets.QLabel("Reject file types (--reject, comma sep):"), 6, 0, 1, 2)
        self.reject_edit = QtWidgets.QLineEdit()
        self.reject_edit.setPlaceholderText("e.g. gif,tmp,php")
        options_layout.addWidget(self.reject_edit, 6, 2, 1, 2)

        # Extra include/reject regex
        options_layout.addWidget(QtWidgets.QLabel("Reject regex (--reject-regex):"), 7, 0)
        self.reject_regex = QtWidgets.QLineEdit()
        options_layout.addWidget(self.reject_regex, 7, 1, 1, 3)

        options_layout.addWidget(QtWidgets.QLabel("User-Agent (--user-agent):"), 8, 0)
        self.user_agent_edit = QtWidgets.QLineEdit()
        self.user_agent_edit.setPlaceholderText("e.g. Mozilla/5.0 (optional)")
        options_layout.addWidget(self.user_agent_edit, 8, 1, 1, 3)

        self.checkbox_span_hosts = QtWidgets.QCheckBox("Span hosts (-H)")
        options_layout.addWidget(self.checkbox_span_hosts, 9, 0)
        self.checkbox_follow_ftp = QtWidgets.QCheckBox("Follow FTP links (-F)")
        options_layout.addWidget(self.checkbox_follow_ftp, 9, 1)
        self.checkbox_do_not_clobber = QtWidgets.QCheckBox("Do not clobber (-nc)")
        options_layout.addWidget(self.checkbox_do_not_clobber, 9, 2)

        # Save presets buttons
        self.preset_combo = QtWidgets.QComboBox()
        options_layout.addWidget(QtWidgets.QLabel("Presets:"), 10, 0)
        options_layout.addWidget(self.preset_combo, 10, 1)
        self.save_preset_btn = QtWidgets.QPushButton("Save Preset")
        self.save_preset_btn.clicked.connect(self.save_preset)
        options_layout.addWidget(self.save_preset_btn, 10, 2)
        self.load_preset_btn = QtWidgets.QPushButton("Load Preset")
        self.load_preset_btn.clicked.connect(self.load_preset)
        options_layout.addWidget(self.load_preset_btn, 10, 3)

        layout.addWidget(options_group)

        # Command preview and edit
        cmd_group = QtWidgets.QGroupBox("Wget command preview")
        cmd_layout = QtWidgets.QVBoxLayout()
        cmd_group.setLayout(cmd_layout)
        self.cmd_preview = QtWidgets.QPlainTextEdit()
        self.cmd_preview.setReadOnly(False)  # allow editing
        self.cmd_preview.setMaximumHeight(90)
        cmd_layout.addWidget(self.cmd_preview)
        self.update_cmd_btn = QtWidgets.QPushButton("Rebuild Command from UI")
        self.update_cmd_btn.clicked.connect(self.rebuild_command)
        cmd_layout.addWidget(self.update_cmd_btn)
        layout.addWidget(cmd_group)

        # Buttons: Start / Stop / Clear Log
        buttons_h = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start Download")
        self.start_btn.clicked.connect(self.start_download)
        buttons_h.addWidget(self.start_btn)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop_download)
        self.stop_btn.setEnabled(False)
        buttons_h.addWidget(self.stop_btn)
        self.clear_log_btn = QtWidgets.QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(lambda: self.log.clear())
        buttons_h.addWidget(self.clear_log_btn)
        layout.addLayout(buttons_h)

        # Progress / status
        prog_h = QtWidgets.QHBoxLayout()
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        prog_h.addWidget(self.progress_bar)
        self.status_label = QtWidgets.QLabel("Idle")
        prog_h.addWidget(self.status_label)
        layout.addLayout(prog_h)

        # log viewer
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        font = QtGui.QFont("Courier", 10)
        self.log.setFont(font)
        layout.addWidget(self.log, stretch=3)

        # bottom: quick tips
        tips = QtWidgets.QLabel(
            "<b>Tips:</b> For open directory downloads use -r -np -nH --cut-dirs to avoid creating deep host dirs. "
            "Use --accept to restrict file types. Use -c to resume. If wget isn't found, install it and ensure it's in PATH."
        )
        tips.setWordWrap(True)
        layout.addWidget(tips)

        # load presets
        self.presets = {}
        self.load_presets_from_file()

        # initial command
        self.rebuild_command()

        # check wget presence
        self.wget_path = shutil.which("wget")
        if not self.wget_path:
            self.append_log("WARNING: 'wget' not found in PATH. Please install wget (or use WSL on Windows).")
        else:
            self.append_log(f"Found wget at: {self.wget_path}")

    # ---------- UI helper methods ----------
    def append_log(self, line: str):
        self.log.appendPlainText(line)
        # keep scrolling to end
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def browse_dest(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select destination folder", self.dest_edit.text())
        if d:
            self.dest_edit.setText(d)

    def gather_options(self):
        """
        Collect UI options and return a list of arguments for wget.
        """
        opts = []

        # Basic recommended flags for directory scraping:
        # -r recursion, -np no-parent, -nH no host dir, --cut-dirs=N, -c resume, -N timestamping, -m mirror
        if self.checkbox_mirror.isChecked():
            opts += ["-m"]
        else:
            if self.checkbox_recursive.isChecked():
                opts += ["-r"]
            if self.checkbox_no_parent.isChecked():
                opts += ["-np"]
        if self.checkbox_no_host_dir.isChecked():
            opts += ["-nH"]

        cut_dirs = self.spin_cutdirs.value()
        if cut_dirs > 0:
            opts += [f"--cut-dirs={cut_dirs}"]

        depth = self.spin_depth.value()
        # depth 0 means no recursion depth option (wget default is 5 for -r), but user likely wants explicit
        if self.checkbox_recursive.isChecked() and depth > 0 and not self.checkbox_mirror.isChecked():
            opts += [f"-l", str(depth)]

        if self.checkbox_timestamp.isChecked():
            opts += ["-N"]

        if self.checkbox_continue.isChecked():
            opts += ["-c"]

        if self.checkbox_do_not_clobber.isChecked():
            opts += ["-nc"]

        # rate limit
        rate = self.limit_rate_edit.text().strip()
        if rate:
            # user might type digits: convert to k
            opts += [f"--limit-rate={rate}"]

        # retries and timeout
        tries = self.spin_retries.value()
        if tries > 0:
            opts += [f"--tries={tries}"]
        timeout = self.spin_timeout.value()
        if timeout > 0:
            opts += [f"--timeout={timeout}"]

        # accept/reject
        accept = self.accept_edit.text().strip()
        if accept:
            # accept expects comma separated list; pass as-is
            opts += [f"--accept={accept}"]
        reject = self.reject_edit.text().strip()
        if reject:
            opts += [f"--reject={reject}"]
        rej_rgx = self.reject_regex.text().strip()
        if rej_rgx:
            opts += [f"--reject-regex={rej_rgx}"]

        ua = self.user_agent_edit.text().strip()
        if ua:
            opts += [f"--user-agent={ua}"]

        if self.checkbox_span_hosts.isChecked():
            opts += ["-H"]
        if self.checkbox_follow_ftp.isChecked():
            opts += ["-F"]

        # ensure we follow links necessary for directory listing (HTML)
        # by default wget will accept html; we won't force accept, but we can add -r -A if accept set.

        # add other useful defaults: show progress=dot:mega for better parsing? Keep default.
        # We avoid adding -P (directory) here; we'll set working directory when running.
        return opts

    def rebuild_command(self):
        """
        Build a full command string and put it in the command preview.
        The preview is editable so advanced users can tweak it.
        """
        # Get URLs from list
        urls = []
        for i in range(self.url_list.count()):
            urls.append(self.url_list.item(i).text())
        
        if not urls:
            url = "<URL>"
        else:
            url = urls[0] if len(urls) == 1 else f"<{len(urls)} URLs>"
        
        opts = self.gather_options()
        cmd = []
        # use wget from PATH if available
        wget_cmd = shutil.which("wget") or "wget"
        cmd.append(wget_cmd)
        # common safety flags: --no-verbose? We keep verbose to get progress lines.
        cmd += opts
        # Save files relative to given dest, we will set -P when running.
        # Ensure we don't clobber by default? Respect user's options.
        cmd_line = " ".join([self._shell_quote(x) for x in cmd] + [self._shell_quote(url)])
        self.cmd_preview.setPlainText(cmd_line)

    @staticmethod
    def _shell_quote(s: str) -> str:
        # minimal quoting for spaces and special chars
        if not s:
            return "''"
        if re.search(r"\s|'|\"", s):
            return "'" + s.replace("'", "'\"'\"'") + "'"
        return s

    # ---------- Presets ----------
    def load_presets_from_file(self):
        try:
            if os.path.exists(PRESETS_FILE):
                with open(PRESETS_FILE, "r", encoding="utf-8") as f:
                    self.presets = json.load(f)
            else:
                self.presets = {}
        except Exception:
            self.presets = {}
        self.refresh_preset_combo()

    def save_presets_to_file(self):
        try:
            with open(PRESETS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.presets, f, indent=2)
        except Exception as e:
            self.append_log(f"Failed to save presets: {e}")

    def refresh_preset_combo(self):
        self.preset_combo.clear()
        self.preset_combo.addItem("-- select --")
        for name in sorted(self.presets.keys()):
            self.preset_combo.addItem(name)

    def save_preset(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Preset name", "Enter preset name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        # gather UI state
        urls = []
        for i in range(self.url_list.count()):
            urls.append(self.url_list.item(i).text())
        
        preset = {
            "urls": urls,  # Changed from single URL to list
            "dest": self.dest_edit.text(),
            "recursive": self.checkbox_recursive.isChecked(),
            "no_parent": self.checkbox_no_parent.isChecked(),
            "mirror": self.checkbox_mirror.isChecked(),
            "depth": self.spin_depth.value(),
            "cutdirs": self.spin_cutdirs.value(),
            "no_host_dir": self.checkbox_no_host_dir.isChecked(),
            "timestamp": self.checkbox_timestamp.isChecked(),
            "continue": self.checkbox_continue.isChecked(),
            "limit_rate": self.limit_rate_edit.text(),
            "tries": self.spin_retries.value(),
            "timeout": self.spin_timeout.value(),
            "accept": self.accept_edit.text(),
            "reject": self.reject_edit.text(),
            "rej_regex": self.reject_regex.text(),
            "user_agent": self.user_agent_edit.text(),
            "span_hosts": self.checkbox_span_hosts.isChecked(),
            "follow_ftp": self.checkbox_follow_ftp.isChecked(),
            "do_not_clobber": self.checkbox_do_not_clobber.isChecked(),
        }
        self.presets[name] = preset
        self.save_presets_to_file()
        self.refresh_preset_combo()
        self.append_log(f"Preset '{name}' saved.")

    def load_preset(self):
        name = self.preset_combo.currentText()
        if not name or name == "-- select --":
            self.append_log("Select a preset to load.")
            return
        p = self.presets.get(name)
        if not p:
            self.append_log("Preset data missing.")
            return
        # apply
        # Handle both old (single URL) and new (URL list) format
        self.url_list.clear()
        if "urls" in p:
            for url in p["urls"]:
                self.url_list.addItem(url)
        elif "url" in p:
            # Legacy support for old presets
            if p["url"]:
                self.url_list.addItem(p["url"])
        
        self.dest_edit.setText(p.get("dest", os.path.expanduser("~")))
        self.checkbox_recursive.setChecked(p.get("recursive", True))
        self.checkbox_no_parent.setChecked(p.get("no_parent", True))
        self.checkbox_mirror.setChecked(p.get("mirror", False))
        self.spin_depth.setValue(p.get("depth", 5))
        self.spin_cutdirs.setValue(p.get("cutdirs", 0))
        self.checkbox_no_host_dir.setChecked(p.get("no_host_dir", True))
        self.checkbox_timestamp.setChecked(p.get("timestamp", False))
        self.checkbox_continue.setChecked(p.get("continue", True))
        self.limit_rate_edit.setText(p.get("limit_rate", ""))
        self.spin_retries.setValue(p.get("tries", 20))
        self.spin_timeout.setValue(p.get("timeout", 30))
        self.accept_edit.setText(p.get("accept", ""))
        self.reject_edit.setText(p.get("reject", ""))
        self.reject_regex.setText(p.get("rej_regex", ""))
        self.user_agent_edit.setText(p.get("user_agent", ""))
        self.checkbox_span_hosts.setChecked(p.get("span_hosts", False))
        self.checkbox_follow_ftp.setChecked(p.get("follow_ftp", False))
        self.checkbox_do_not_clobber.setChecked(p.get("do_not_clobber", False))
        self.rebuild_command()
        self.append_log(f"Loaded preset '{name}'.")

    # ---------- Run / Control ----------
    def add_url(self):
        """Add URL from input field to the list."""
        url = self.url_input.text().strip()
        if url:
            # Check if URL already exists
            existing = [self.url_list.item(i).text() for i in range(self.url_list.count())]
            if url not in existing:
                self.url_list.addItem(url)
                self.url_input.clear()
                self.rebuild_command()
            else:
                QtWidgets.QMessageBox.information(self, "Duplicate URL", "This URL is already in the list.")
        else:
            QtWidgets.QMessageBox.warning(self, "Empty URL", "Please enter a URL.")
    
    def remove_url(self):
        """Remove selected URL from the list."""
        current_item = self.url_list.currentItem()
        if current_item:
            self.url_list.takeItem(self.url_list.row(current_item))
            self.rebuild_command()
        else:
            QtWidgets.QMessageBox.information(self, "No Selection", "Please select a URL to remove.")
    
    def search_sources(self):
        """Search all URL sources for files matching the search text."""
        search_text = self.search_input.text().strip().lower()
        if not search_text:
            QtWidgets.QMessageBox.warning(self, "Empty Search", "Please enter search text.")
            return
        
        urls = []
        for i in range(self.url_list.count()):
            urls.append(self.url_list.item(i).text())
        
        if not urls:
            QtWidgets.QMessageBox.warning(self, "No URLs", "Please add at least one URL source.")
            return
        
        # Show progress dialog
        progress = QtWidgets.QProgressDialog("Searching URLs...", "Cancel", 0, len(urls), self)
        progress.setWindowModality(QtCore.Qt.WindowModal)
        progress.setMinimumDuration(0)
        
        matching_files = []
        
        for idx, url in enumerate(urls):
            if progress.wasCanceled():
                break
            
            progress.setValue(idx)
            progress.setLabelText(f"Searching {idx + 1}/{len(urls)}: {url}")
            QtWidgets.QApplication.processEvents()
            
            try:
                # Fetch directory listing
                self.append_log(f"Fetching directory listing from: {url}")
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    html = response.read().decode('utf-8', errors='ignore')
                
                # Parse links
                parser = DirectoryParser(url)
                parser.feed(html)
                
                # Filter matching files
                for link in parser.links:
                    # Get filename from URL (cross-platform compatible)
                    parsed = urllib.parse.urlparse(link)
                    filename = parsed.path.split('/')[-1].lower()
                    if filename and search_text in filename:
                        matching_files.append(link)
                        self.append_log(f"  Found: {link}")
                
            except Exception as e:
                self.append_log(f"Error fetching {url}: {e}")
        
        progress.setValue(len(urls))
        
        if matching_files:
            self.append_log(f"\nSearch complete. Found {len(matching_files)} matching files.")
            # Show results dialog
            dialog = SearchResultsDialog(matching_files, self)
            if dialog.exec_() == QtWidgets.QDialog.Accepted:
                selected = dialog.get_selected_urls()
                if selected:
                    self.append_log(f"User selected {len(selected)} files for download.")
                    # Create wget commands for selected files
                    self.create_multi_wget_commands(selected)
                else:
                    self.append_log("No files selected.")
            else:
                self.append_log("Search cancelled by user.")
        else:
            QtWidgets.QMessageBox.information(self, "No Results", 
                                             f"No files matching '{search_text}' were found.")
            self.append_log("Search complete. No matching files found.")
    
    def create_multi_wget_commands(self, urls):
        """Generate wget commands for multiple URLs."""
        dest = self.dest_edit.text().strip()
        if not dest:
            QtWidgets.QMessageBox.warning(self, "Missing destination", 
                                        "Please choose a destination folder first.")
            return
        
        if not os.path.exists(dest):
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Destination error", 
                                             f"Failed to create destination folder: {e}")
                return
        
        # Build wget commands
        wget_cmd = shutil.which("wget") or "wget"
        
        if len(urls) == 1:
            # Single file - show simple command
            cmd_parts = [wget_cmd, "-P", dest]
            if self.checkbox_continue.isChecked():
                cmd_parts.append("-c")
            cmd_parts.append(urls[0])
            command = " ".join([self._shell_quote(x) for x in cmd_parts])
            self.cmd_preview.setPlainText(command)
            self.append_log(f"\nGenerated wget command:\n{command}\n")
        else:
            # Multiple files - show all commands
            commands = []
            for url in urls:
                cmd_parts = [wget_cmd, "-P", dest]
                if self.checkbox_continue.isChecked():
                    cmd_parts.append("-c")
                cmd_parts.append(url)
                commands.append(" ".join([self._shell_quote(x) for x in cmd_parts]))
            
            full_command = "\n".join(commands)
            self.cmd_preview.setPlainText(full_command)
            self.append_log(f"\nGenerated {len(urls)} wget commands:\n{full_command}\n")
        
        # Ask if user wants to execute
        reply = QtWidgets.QMessageBox.question(self, "Execute Commands?",
                                             f"Execute wget for {len(urls)} file(s)?",
                                             QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if reply == QtWidgets.QMessageBox.Yes:
            self.execute_multi_wget(urls)
    
    def execute_multi_wget(self, urls):
        """Execute wget for multiple URLs sequentially.
        Note: Uses synchronous subprocess calls which will block the UI during downloads.
        For better UX with many files, consider implementing async downloads.
        """
        dest = self.dest_edit.text().strip()
        wget_cmd = shutil.which("wget") or "wget"
        
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Downloading files...")
        self.progress_bar.setValue(0)
        
        total = len(urls)
        for idx, url in enumerate(urls):
            # Extract filename from URL (cross-platform compatible)
            parsed = urllib.parse.urlparse(url)
            filename = parsed.path.split('/')[-1] or 'file'
            
            self.append_log(f"\nDownloading {idx + 1}/{total}: {filename}")
            
            # Build argv for this file
            argv = [wget_cmd, "-P", dest]
            if self.checkbox_continue.isChecked():
                argv.append("-c")
            argv.append(url)
            
            # Execute synchronously using subprocess
            try:
                result = subprocess.run(argv, cwd=dest, capture_output=True, text=True, timeout=300)
                self.append_log(result.stdout)
                if result.stderr:
                    self.append_log(result.stderr)
                if result.returncode == 0:
                    self.append_log(f"Successfully downloaded: {filename}")
                else:
                    self.append_log(f"Failed to download: {url} (exit code {result.returncode})")
            except subprocess.TimeoutExpired:
                self.append_log(f"Timeout downloading: {url}")
            except Exception as e:
                self.append_log(f"Error downloading {url}: {e}")
            
            # Update progress
            progress = int((idx + 1) / total * 100)
            self.progress_bar.setValue(progress)
        
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Completed")
        self.append_log("\nAll downloads completed.")

    # ---------- Run / Control ----------
    def start_download(self):
        if not shutil.which("wget"):
            QtWidgets.QMessageBox.warning(self, "wget missing", "wget not found in PATH. Install it and try again.")
            return

        # Get URLs from list
        urls = []
        for i in range(self.url_list.count()):
            urls.append(self.url_list.item(i).text())
        
        if not urls:
            QtWidgets.QMessageBox.warning(self, "Missing URL", "Please add at least one URL to download.")
            return

        dest = self.dest_edit.text().strip()
        if not dest:
            QtWidgets.QMessageBox.warning(self, "Missing destination", "Please choose a destination folder.")
            return
        if not os.path.exists(dest):
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Destination error", f"Failed to create destination folder: {e}")
                return

        # Use first URL for single URL download, or ask user for multiple
        if len(urls) > 1:
            reply = QtWidgets.QMessageBox.question(self, "Multiple URLs",
                                                 f"Download from all {len(urls)} URLs?",
                                                 QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            if reply == QtWidgets.QMessageBox.No:
                return
        
        url = urls[0]  # For now, use first URL for the runner

        # Build argv list for QProcess (no shell)
        argv = []
        wget_cmd = shutil.which("wget") or "wget"
        argv.append(wget_cmd)
        # get options from UI
        opts = self.gather_options()
        argv.extend(opts)
        # Save into dest folder (use -P)
        argv.append("-P")
        argv.append(dest)
        # Add --restrict-file-names=nocontrol to avoid weird characters (optional)
        argv.append("--restrict-file-names=nocontrol")
        # Add URL
        argv.append(url)

        # Allow user to edit full command before running (optional)
        # but we will use argv array built above. Update preview
        self.cmd_preview.setPlainText(" ".join([self._shell_quote(x) for x in argv]))

        # Start process
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_label.setText("Running...")
        self.progress_bar.setValue(0)
        self.log.appendPlainText(f"Launching wget... (cwd={dest})")
        self.runner.start(argv, working_dir=dest)

    def stop_download(self):
        self.runner.stop()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_label.setText("Stopped by user")

    def on_progress(self, info: dict):
        # info may include percent, speed, eta
        pct = info.get("percent")
        if pct is not None:
            try:
                self.progress_bar.setValue(int(pct))
                txt = f"{pct}%"
                if info.get("speed"):
                    txt += f" · {info.get('speed')}"
                if info.get("eta"):
                    txt += f" · ETA {info.get('eta')}"
                self.status_label.setText(txt)
            except Exception:
                pass

    def on_finished(self, exitCode, exitStatus):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if exitCode == 0:
            self.status_label.setText("Completed")
            self.append_log("Download completed successfully.")
        else:
            self.status_label.setText(f"Finished with exit code {exitCode}")
            self.append_log(f"wget finished with exit code {exitCode}.")

# ---------- Main ----------
def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

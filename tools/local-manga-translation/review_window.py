"""Side-by-side human review and real-time visual text editor for manga translation."""
import json
from pathlib import Path
import traceback

from PyQt6 import QtCore, QtGui, QtWidgets
from page_fast_renderer import (resolve_page_context, render_page_image,
                                save_and_publish_page, validate_published_page)


class ReviewCanvas(QtWidgets.QGraphicsView):
    regionSelected = QtCore.pyqtSignal(int)
    zoomRequested = QtCore.pyqtSignal(float)
    positionChanged = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setBackgroundBrush(QtGui.QColor('#252a32'))
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(self.DragMode.ScrollHandDrag)
        self.setMinimumSize(160, 180)
        self.setTransformationAnchor(self.ViewportAnchor.NoAnchor)
        self.regions, self.overlays = {}, {}
        for bar in (self.horizontalScrollBar(), self.verticalScrollBar()):
            bar.valueChanged.connect(lambda: self.positionChanged.emit(self))

    def load_image(self, path):
        pixmap = QtGui.QPixmap(str(path))
        if pixmap.isNull():
            raise ValueError('無法開啟審查圖片：' + str(path))
        self.scene().clear()
        self.overlays = {}
        self.scene().addPixmap(pixmap)
        self.setSceneRect(QtCore.QRectF(pixmap.rect()))

    def update_pixmap(self, pixmap):
        if pixmap.isNull():
            return
        for item in self.scene().items():
            if isinstance(item, QtWidgets.QGraphicsPixmapItem):
                self.scene().removeItem(item)
        pix = self.scene().addPixmap(pixmap)
        pix.setZValue(0)
        self.setSceneRect(QtCore.QRectF(pixmap.rect()))

    def mark_regions(self, regions, field, selected):
        for item in self.overlays.values():
            self.scene().removeItem(item)
        self.regions, self.overlays = {}, {}
        for region in regions:
            sid = region['id']
            x0, y0, x1, y1 = region[field]
            rect = QtCore.QRectF(x0, y0, x1 - x0, y1 - y0)
            self.regions[sid] = rect
            color = QtGui.QColor('#52c7ff' if sid == selected else '#ffb740')
            pen = QtGui.QPen(color, 3)
            pen.setCosmetic(True)
            overlay = self.scene().addRect(rect, pen, QtGui.QBrush(QtGui.QColor(color.red(), color.green(), color.blue(), 18)))
            overlay.setZValue(2 if sid == selected else 1)
            overlay.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            badge = QtWidgets.QGraphicsRectItem(0, -22, 40, 22, overlay)
            badge.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            badge.setBrush(color)
            badge.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
            badge.setPos(x0, y0)
            badge.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            label = QtWidgets.QGraphicsSimpleTextItem(f'#{sid}', badge)
            label.setFont(QtGui.QFont('Microsoft JhengHei', 10, QtGui.QFont.Weight.Bold))
            label.setBrush(QtGui.QColor('#16202b'))
            label.setPos(3, -21)
            label.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
            self.overlays[sid] = overlay

    def wheelEvent(self, event):
        self.zoomRequested.emit(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            point = self.mapToScene(event.position().toPoint())
            hits = [(rect.width() * rect.height(), sid) for sid, rect in self.regions.items() if rect.contains(point)]
            if hits:
                self.regionSelected.emit(min(hits)[1])
        super().mousePressEvent(event)


class _ReviewWorker(QtCore.QThread):
    def __init__(self, action, parent):
        super().__init__(parent)
        self.action, self.result, self.error = action, None, None

    def run(self):
        try:
            self.result = self.action()
        except Exception:
            self.error = traceback.format_exc()


class ReviewDialog(QtWidgets.QDialog):
    jobChanged = QtCore.pyqtSignal()

    def __init__(self, pages, load_page, confirm, publish, parent=None, selected_path=None, output_dir=None):
        super().__init__(parent)
        self.load_page, self.confirm, self.publish = load_page, confirm, publish
        self.output_dir = Path(output_dir).resolve() if output_dir else None
        self.pages = list(pages)
        self.view = None
        self.worker = None
        self.busy = False
        self.syncing = False
        self.fit_enabled = True
        self.selected_id = None
        self.page_overrides = {}
        self.fast_context = None

        # 防抖即時重繪計時器
        self.render_timer = QtCore.QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self.trigger_fast_rerender)

        self.setWindowTitle('人工審查與單頁視覺精修工作台')
        self.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(780, 680)
        available = self.screen().availableGeometry()
        self.resize(min(1480, available.width() - 40), min(980, available.height() - 40))

        layout = QtWidgets.QVBoxLayout(self)

        # 頂部工具列
        toolbar = QtWidgets.QHBoxLayout()
        self.page_selector = QtWidgets.QComboBox()
        self.page_selector.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.page_selector.setMinimumContentsLength(16)
        for page in self.pages:
            self.page_selector.addItem(f"第 {page.get('page_index', '—')} 頁 · {page.get('source_name') or page['normalized_name']}", page['details_path'])
        toolbar.addWidget(self.page_selector, 1)

        self.zoom_out = QtWidgets.QPushButton('縮小 (-)')
        self.zoom_in = QtWidgets.QPushButton('放大 (+)')
        self.fit_button = QtWidgets.QPushButton('完整頁面 (A)')
        self.locate = QtWidgets.QPushButton('放大目前框 (F)')
        self.zoom_label = QtWidgets.QLabel()
        for widget in (self.zoom_out, self.zoom_in, self.fit_button, self.locate, self.zoom_label):
            toolbar.addWidget(widget)
        layout.addLayout(toolbar)

        self.hint = QtWidgets.QLabel(
            '【操作提示】點選對話框即可直接改字與調大小（即時預覽）。'
            '快捷鍵：[Ctrl+S] 儲存發布本頁 · [Space] 本框通過 · [Ctrl+Enter] 整頁通過 · [F] 聚焦框 · [A] 全頁 · 支援 Enter 手動換行。'
        )
        self.hint.setStyleSheet('color: #64b5f6; font-size: 11px; padding: 2px 0;')
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        # 左右圖像面板
        images = QtWidgets.QWidget()
        image_layout = QtWidgets.QHBoxLayout(images)
        image_layout.setContentsMargins(0, 0, 0, 0)
        self.left, self.right = ReviewCanvas(), ReviewCanvas()
        for title, canvas in (('原始日文圖', self.left), ('中文即時預覽圖（所見即所得）', self.right)):
            panel = QtWidgets.QWidget()
            column = QtWidgets.QVBoxLayout(panel)
            column.setContentsMargins(0, 0, 0, 0)
            label = QtWidgets.QLabel(title)
            label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet('font-weight: bold; padding: 4px; color: #eceff1;')
            column.addWidget(label)
            column.addWidget(canvas, 1)
            image_layout.addWidget(panel, 1)
            canvas.regionSelected.connect(self.select_region)
            canvas.zoomRequested.connect(self.zoom)
            canvas.positionChanged.connect(self.sync_position)
        self.splitter.addWidget(images)

        # 下方控制面板 (表格 + 精修控制列)
        bottom_panel = QtWidgets.QWidget()
        bottom_layout = QtWidgets.QVBoxLayout(bottom_panel)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(6)

        # 表格
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['編號', '原始日文', '繁體中文'])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        for column in (1, 2):
            self.table.horizontalHeader().setSectionResizeMode(column, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(70)
        self.table.setMaximumHeight(130)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda _: self.focus_region())
        bottom_layout.addWidget(self.table)

        # 精修控制群組
        self.editor_box = QtWidgets.QGroupBox('選取框文字與排版精修（修改立即更新預覽）')
        self.editor_box.setStyleSheet('QGroupBox { font-weight: bold; color: #81c784; border: 1px solid #455a64; border-radius: 4px; margin-top: 6px; padding-top: 10px; }')
        box_layout = QtWidgets.QHBoxLayout(self.editor_box)
        box_layout.setContentsMargins(8, 8, 8, 8)
        box_layout.setSpacing(10)

        # 中文編輯輸入框
        editor_col = QtWidgets.QVBoxLayout()
        editor_header = QtWidgets.QHBoxLayout()
        self.editor_label = QtWidgets.QLabel('繁體中文內容（支援手動 Enter 換行）：')
        self.editor_label.setStyleSheet('color: #cfd8dc; font-size: 11px;')
        editor_header.addWidget(self.editor_label)
        editor_header.addStretch()
        editor_col.addLayout(editor_header)

        self.text_editor = QtWidgets.QPlainTextEdit()
        self.text_editor.setPlaceholderText('請點選文字框進行編輯…')
        self.text_editor.setFixedHeight(68)
        self.text_editor.setStyleSheet('font-size: 13px; font-family: "Microsoft JhengHei", sans-serif;')
        self.text_editor.textChanged.connect(self.on_text_edited)
        editor_col.addWidget(self.text_editor)
        box_layout.addLayout(editor_col, 3)

        # 右側微調按鈕群組
        controls_col = QtWidgets.QVBoxLayout()
        controls_col.setSpacing(4)

        row1 = QtWidgets.QHBoxLayout()
        self.btn_font_dec = QtWidgets.QPushButton('A- 縮小字體')
        self.lbl_scale = QtWidgets.QLabel('100%')
        self.lbl_scale.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_scale.setFixedWidth(46)
        self.lbl_scale.setStyleSheet('font-weight: bold; color: #ffe082;')
        self.btn_font_inc = QtWidgets.QPushButton('A+ 放大字體')
        self.btn_toggle_dir = QtWidgets.QPushButton('直排/橫排切換')
        for w in (self.btn_font_dec, self.lbl_scale, self.btn_font_inc, self.btn_toggle_dir):
            row1.addWidget(w)
        controls_col.addLayout(row1)

        row2 = QtWidgets.QHBoxLayout()
        self.btn_box_expand = QtWidgets.QPushButton('框擴大 (+15%)')
        self.btn_box_shrink = QtWidgets.QPushButton('框收縮 (-15%)')
        self.btn_reset_block = QtWidgets.QPushButton('重設本框')
        for w in (self.btn_box_expand, self.btn_box_shrink, self.btn_reset_block):
            row2.addWidget(w)
        controls_col.addLayout(row2)

        # 儲存發布大按鈕
        self.btn_save_publish = QtWidgets.QPushButton('儲存並發布本頁修改 (Ctrl+S)')
        self.btn_save_publish.setStyleSheet('background-color: #2e7d32; color: #ffffff; font-weight: bold; padding: 6px; border-radius: 4px;')
        self.btn_save_publish.clicked.connect(self.save_and_publish_current)
        controls_col.addWidget(self.btn_save_publish)

        box_layout.addLayout(controls_col, 2)
        bottom_layout.addWidget(self.editor_box)

        # 連接微調動作
        self.btn_font_dec.clicked.connect(lambda: self.adjust_scale(-0.1))
        self.btn_font_inc.clicked.connect(lambda: self.adjust_scale(0.1))
        self.btn_toggle_dir.clicked.connect(self.toggle_direction)
        self.btn_box_expand.clicked.connect(lambda: self.adjust_box(0.15))
        self.btn_box_shrink.clicked.connect(lambda: self.adjust_box(-0.15))
        self.btn_reset_block.clicked.connect(self.reset_current_block)

        self.splitter.addWidget(bottom_panel)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([560, 220])
        layout.addWidget(self.splitter, 1)

        self.progress = QtWidgets.QLabel()
        self.progress.setWordWrap(True)
        layout.addWidget(self.progress)

        self.message = QtWidgets.QPlainTextEdit()
        self.message.setReadOnly(True)
        self.message.setMaximumHeight(80)
        self.message.hide()
        layout.addWidget(self.message)

        # 底部動作按鈕
        actions = QtWidgets.QHBoxLayout()
        self.approve_one = QtWidgets.QPushButton('本框通過 (Space)')
        self.approve_all = QtWidgets.QPushButton('整頁通過並輸出 (Ctrl+Enter)')
        self.next_page = QtWidgets.QPushButton('下一待審頁 (N)')
        self.close_button = QtWidgets.QPushButton('關閉 (Esc)')
        for button in (self.approve_one, self.approve_all, self.next_page, self.close_button):
            actions.addWidget(button)
        layout.addLayout(actions)

        self.approve_one.clicked.connect(lambda: self.approve(False))
        self.approve_all.clicked.connect(lambda: self.approve(True))
        self.next_page.clicked.connect(self.next_pending_page)
        self.close_button.clicked.connect(self.close)
        self.fit_button.clicked.connect(self.fit_page)
        self.locate.clicked.connect(self.focus_region)
        self.zoom_in.clicked.connect(lambda: self.zoom(1.2))
        self.zoom_out.clicked.connect(lambda: self.zoom(1 / 1.2))

        if selected_path:
            index = self.page_selector.findData(selected_path)
            if index >= 0:
                self.page_selector.setCurrentIndex(index)
        self.page_selector.currentIndexChanged.connect(self.load_current)
        self.load_current()

    def load_current(self):
        if self.busy:
            return
        path = self.page_selector.currentData()
        self.view = None
        self.fast_context = None
        self.page_overrides = {}
        self.message.hide()
        if not path:
            self.update_controls()
            return
        try:
            view = self.load_page(Path(path))
            self.set_view(view, new_page=True)
            # 嘗試載入快速渲染上下文
            page_id = view.get('normalized_name') or view.get('page') or view.get('source_name')
            out_dir = self.output_dir
            if not out_dir:
                try:
                    from folder_translator import review_output
                    out_dir = review_output(Path(path))
                except Exception:
                    pass
            if out_dir and page_id:
                try:
                    self.fast_context = resolve_page_context(out_dir, page_id)
                    self.page_overrides = dict(self.fast_context.get('existing_overrides', {}))
                except Exception as err:
                    print(f"快速渲染上下文解析略過: {err}")
            self.update_controls()
        except Exception as error:
            self.left.scene().clear()
            self.right.scene().clear()
            self.table.setRowCount(0)
            self.message.setPlainText('無法載入待審頁，未核准任何內容。\n' + str(error))
            self.message.show()
            self.update_controls()

    def set_view(self, view, *, new_page=False):
        self.view = view
        self.syncing = True
        if new_page:
            self.left.load_image(view['source_path'])
            self.right.load_image(view['preview_path'])
        remaining = self.remaining()
        previous = self.selected_id
        self.table.blockSignals(True)
        self.table.setRowCount(len(remaining))
        for index, region in enumerate(remaining):
            for column, value in enumerate((f"#{region['id']}", region['japanese'], region['translation'])):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setData(QtCore.Qt.ItemDataRole.UserRole, region['id'])
                item.setToolTip(str(value))
                self.table.setItem(index, column, item)
        self.table.resizeRowsToContents()
        self.selected_id = previous if previous in {row['id'] for row in remaining} else (remaining[0]['id'] if remaining else None)
        if self.selected_id is not None:
            self.table.selectRow(next(i for i, row in enumerate(remaining) if row['id'] == self.selected_id))
        self.table.blockSignals(False)
        self.redraw_markers()
        self.syncing = False
        total = len(view['regions'])
        self.progress.setText(f"已確認 {total - len(remaining)} / {total} 框，待確認 {len(remaining)} 框。"
                              + (' 全部確認後會輸出本頁。' if remaining else ' 本頁已確認，等待輸出完成。'))
        self.update_editor_from_selection()
        self.update_controls()
        if new_page:
            self.fit_enabled = True
            QtCore.QTimer.singleShot(0, self.fit_page)

    def remaining(self):
        if not self.view:
            return []
        confirmed = set(self.view['confirmed_ids'])
        return [row for row in self.view['regions'] if row['id'] not in confirmed]

    def redraw_markers(self):
        remaining = self.remaining()
        self.left.mark_regions(remaining, 'source_rect', self.selected_id)
        self.right.mark_regions(remaining, 'target_rect', self.selected_id)

    def select_region(self, sid):
        for index in range(self.table.rowCount()):
            if self.table.item(index, 0).data(QtCore.Qt.ItemDataRole.UserRole) == sid:
                self.table.selectRow(index)
                self.table.scrollToItem(self.table.item(index, 0))
                break

    def selection_changed(self):
        item = self.table.item(self.table.currentRow(), 0)
        self.selected_id = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
        self.redraw_markers()
        if self.selected_id is not None:
            self.syncing = True
            for canvas in (self.left, self.right):
                rect = canvas.regions.get(self.selected_id)
                if rect is not None:
                    if self.fit_enabled:
                        canvas.ensureVisible(rect, 24, 28)
                    else:
                        canvas.centerOn(rect.center())
            self.syncing = False
        self.update_editor_from_selection()
        self.update_controls()

    def update_editor_from_selection(self):
        """當選取不同框時，同步載入其文字與樣式參數至精修編輯器。"""
        if self.selected_id is None or not self.view:
            self.text_editor.blockSignals(True)
            self.text_editor.setPlainText('')
            self.text_editor.setEnabled(False)
            self.lbl_scale.setText('100%')
            self.text_editor.blockSignals(False)
            return

        sid_str = str(self.selected_id)
        ovr = self.page_overrides.get(sid_str, {})
        # 尋找原始 region
        orig_row = next((r for r in self.view['regions'] if r['id'] == self.selected_id), None)
        current_text = ovr.get('translation') or (orig_row['translation'] if orig_row else '')
        scale = ovr.get('scale', 1.0)

        self.text_editor.blockSignals(True)
        self.text_editor.setPlainText(current_text)
        self.text_editor.setEnabled(True)
        self.lbl_scale.setText(f'{int(round(scale * 100))}%')
        self.text_editor.blockSignals(False)
        self.editor_label.setText(f'框 #{self.selected_id} 繁體中文內容（支援手動 Enter 換行）：')

    def on_text_edited(self):
        """使用者在輸入框修改文字時，觸發防抖重繪。"""
        if self.selected_id is None:
            return
        new_text = self.text_editor.toPlainText()
        sid_str = str(self.selected_id)
        self.page_overrides.setdefault(sid_str, {})['translation'] = new_text

        # 同步更新表格中的文字
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == self.selected_id:
                self.table.item(row, 2).setText(new_text.replace('\n', ' '))
                break

        # 120ms 防抖即時重繪
        self.render_timer.start(120)

    def adjust_scale(self, delta):
        """微調當前選取框的字級縮放比例 (A+ / A-)。"""
        if self.selected_id is None:
            return
        sid_str = str(self.selected_id)
        current_scale = self.page_overrides.get(sid_str, {}).get('scale', 1.0)
        new_scale = max(0.6, min(2.0, round(current_scale + delta, 2)))
        self.page_overrides.setdefault(sid_str, {})['scale'] = new_scale
        self.lbl_scale.setText(f'{int(round(new_scale * 100))}%')
        self.trigger_fast_rerender()

    def toggle_direction(self):
        """一鍵切換當前選取框的直排 / 橫排。"""
        if self.selected_id is None:
            return
        sid_str = str(self.selected_id)
        current_dir = self.page_overrides.get(sid_str, {}).get('vertical')
        if current_dir is None:
            # 根據目前框長寬比判斷預設
            orig_row = next((r for r in self.view['regions'] if r['id'] == self.selected_id), None)
            if orig_row:
                x0, y0, x1, y1 = orig_row['target_rect']
                current_dir = (y1 - y0) > (x1 - x0) * 1.15
            else:
                current_dir = True
        self.page_overrides.setdefault(sid_str, {})['vertical'] = not current_dir
        self.trigger_fast_rerender()

    def adjust_box(self, factor):
        """按比例擴大或縮小當前選取框的可排字範圍。"""
        if self.selected_id is None:
            return
        sid_str = str(self.selected_id)
        orig_row = next((r for r in self.view['regions'] if r['id'] == self.selected_id), None)
        if not orig_row:
            return
        current_box = self.page_overrides.get(sid_str, {}).get('box') or orig_row['target_rect']
        x0, y0, x1, y1 = current_box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        w, h = (x1 - x0) * (1.0 + factor), (y1 - y0) * (1.0 + factor)
        new_box = [max(0, cx - w / 2), max(0, cy - h / 2), cx + w / 2, cy + h / 2]
        self.page_overrides.setdefault(sid_str, {})['box'] = new_box

        # 更新 target_rect 標記
        orig_row['target_rect'] = new_box
        self.redraw_markers()
        self.trigger_fast_rerender()

    def reset_current_block(self):
        """重設當前選取框至最初狀態。"""
        if self.selected_id is None:
            return
        sid_str = str(self.selected_id)
        self.page_overrides.pop(sid_str, None)
        self.update_editor_from_selection()
        orig_row = next((r for r in self.view['regions'] if r['id'] == self.selected_id), None)
        if orig_row:
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole) == self.selected_id:
                    self.table.item(row, 2).setText(orig_row['translation'])
                    break
        self.trigger_fast_rerender()

    def trigger_fast_rerender(self):
        """在記憶體中執行毫秒級重繪，將最新排版即時更新至右側畫布。"""
        if not self.fast_context:
            return
        try:
            rendered = render_page_image(self.fast_context, self.page_overrides)
            data = rendered.tobytes('raw', 'RGB')
            qimg = QtGui.QImage(data, rendered.width, rendered.height, rendered.width * 3, QtGui.QImage.Format.Format_RGB888)
            pixmap = QtGui.QPixmap.fromImage(qimg)
            self.right.update_pixmap(pixmap)
        except Exception as e:
            print(f"即時重繪出錯: {e}")

    def save_and_publish_current(self):
        """儲存覆寫檔並覆蓋發布最終輸出目錄中的圖片。"""
        if not self.fast_context:
            self.progress.setText('無法取得本頁的發布路徑，請確認工作區狀態。')
            return
        try:
            dest_path, sha = save_and_publish_page(self.fast_context, self.page_overrides)
            self.progress.setText(f'✓ 已成功儲存並發布至 {dest_path.name}！')
            self.jobChanged.emit()
        except Exception as error:
            self.message.setPlainText('儲存發布失敗：\n' + str(error))
            self.message.show()

    def update_controls(self):
        available = not self.busy and self.view is not None
        has_sel = available and self.selected_id is not None
        self.approve_one.setEnabled(has_sel)
        self.approve_one.setText('本框通過並輸出' if len(self.remaining()) == 1 else '本框通過')
        self.approve_all.setEnabled(available)
        self.approve_all.setText('整頁通過並輸出' if self.remaining() else '重試輸出本頁')
        for button in (self.locate, self.zoom_in, self.zoom_out, self.fit_button):
            button.setEnabled(available)
        for button in (self.btn_font_dec, self.btn_font_inc, self.btn_toggle_dir,
                       self.btn_box_expand, self.btn_box_shrink, self.btn_reset_block):
            button.setEnabled(has_sel)
        can_save = available and bool(self.fast_context)
        save_reason = '僅可精修已發布且通過完整性核對的頁面；待審頁請先完成確認。'
        if can_save:
            try:
                validate_published_page(self.fast_context)
            except (OSError, ValueError, TypeError, KeyError) as error:
                can_save = False
                save_reason = str(error)
        self.btn_save_publish.setEnabled(bool(can_save))
        self.btn_save_publish.setToolTip(save_reason)
        self.page_selector.setEnabled(not self.busy)
        self.next_page.setEnabled(not self.busy and self.page_selector.count() > 1)
        self.close_button.setEnabled(not self.busy)
        self.table.setEnabled(not self.busy)

    def sync_position(self, origin):
        if self.syncing or self.view is None:
            return
        self.syncing = True
        target = self.right if origin is self.left else self.left
        target.centerOn(origin.mapToScene(origin.viewport().rect().center()))
        self.syncing = False

    def set_scale(self, scale, centers=None):
        self.syncing = True
        scale = max(0.03, min(8, scale))
        for index, canvas in enumerate((self.left, self.right)):
            center = centers[index] if centers else canvas.mapToScene(canvas.viewport().rect().center())
            canvas.setTransform(QtGui.QTransform.fromScale(scale, scale))
            canvas.centerOn(center)
        self.zoom_label.setText(f'{scale * 100:.0f}%')
        self.syncing = False

    def fit_page(self):
        if not self.view:
            return
        self.fit_enabled = True
        width, height = self.view['size']
        scale = min(min((canvas.viewport().width() - 8) / width, (canvas.viewport().height() - 8) / height)
                    for canvas in (self.left, self.right))
        self.set_scale(scale, [QtCore.QPointF(width / 2, height / 2)] * 2)

    def zoom(self, factor):
        if self.view:
            self.fit_enabled = False
            self.set_scale(self.left.transform().m11() * factor)

    def focus_region(self):
        if self.selected_id is None:
            return
        self.fit_enabled = False
        rects = [canvas.regions[self.selected_id] for canvas in (self.left, self.right)]
        scale = min(min(canvas.viewport().width() / (rect.width() + 120), canvas.viewport().height() / (rect.height() + 120))
                    for canvas, rect in zip((self.left, self.right), rects))
        self.set_scale(min(3, scale), [rect.center() for rect in rects])

    def next_pending_page(self):
        if not self.busy and self.page_selector.count():
            self.page_selector.setCurrentIndex((self.page_selector.currentIndex() + 1) % self.page_selector.count())

    def approve(self, whole_page):
        if self.busy or not self.view:
            return
        view = self.view
        ids = [row['id'] for row in self.remaining()] if whole_page else [self.selected_id]
        if not whole_page and self.selected_id is None:
            return
        path = Path(view['pending_path'])

        def work():
            confirmed = self.confirm(path, ids, view['pending_sha256']) if ids else self.load_page(path)
            complete = len(confirmed['confirmed_ids']) == len(confirmed['regions'])
            if complete:
                self.publish(path)
                # 如果有自訂覆寫，一併套用發布
                if self.fast_context and self.page_overrides:
                    save_and_publish_page(self.fast_context, self.page_overrides)
            return {'view': confirmed, 'published': complete}

        self.busy = True
        self.message.hide()
        self.progress.setText('正在保存人工確認；整頁確認完成後核對並輸出…')
        self.update_controls()
        self.worker = _ReviewWorker(work, self)
        self.worker.finished.connect(self.work_finished)
        self.worker.start()

    def work_finished(self):
        worker = self.worker
        self.worker = None
        self.busy = False
        self.jobChanged.emit()
        if worker.error:
            error = worker.error
            self.load_current()
            self.message.setPlainText('保存或輸出未完成；請查看下列原因。\n' + error)
            self.message.show()
        else:
            self.set_view(worker.result['view'])
            if worker.result['published']:
                name = self.view.get('source_name', self.view['page'])
                self.page_selector.blockSignals(True)
                self.page_selector.removeItem(self.page_selector.currentIndex())
                self.page_selector.blockSignals(False)
                if self.page_selector.count():
                    self.load_current()
                    self.progress.setText(f'{name} 已通過並輸出；目前顯示下一待審頁。 ' + self.progress.text())
                else:
                    self.progress.setText(f'{name} 已通過並輸出。待審清單已清空，畫面上的標記已移除。')
                    self.view = None
                    self.update_controls()
        worker.deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.fit_enabled:
            QtCore.QTimer.singleShot(0, self.fit_page)

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
        else:
            event.accept()

    def keyPressEvent(self, event: QtGui.QKeyEvent):
        if self.busy:
            super().keyPressEvent(event)
            return
        key = event.key()
        modifiers = event.modifiers()
        # Ctrl + S: 儲存發布本頁
        if key == QtCore.Qt.Key.Key_S and bool(modifiers & QtCore.Qt.KeyboardModifier.ControlModifier):
            self.save_and_publish_current()
            event.accept()
            return
        # Ctrl + Enter: 整頁通過並輸出
        elif (key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)) and bool(modifiers & QtCore.Qt.KeyboardModifier.ControlModifier):
            self.approve(True)
            event.accept()
            return
        # Space: 本框通過 (只有在非輸入框焦點時觸發，避免打字時吃掉空格)
        elif key == QtCore.Qt.Key.Key_Space and not self.text_editor.hasFocus():
            if self.selected_id is not None:
                self.approve(False)
                event.accept()
                return
        # F 鍵: 聚焦放大目前框 (非輸入焦點時)
        elif key == QtCore.Qt.Key.Key_F and not self.text_editor.hasFocus():
            self.focus_region()
            event.accept()
            return
        # A 鍵: 完整頁面 (非輸入焦點時)
        elif key == QtCore.Qt.Key.Key_A and not self.text_editor.hasFocus():
            self.fit_page()
            event.accept()
            return
        # N 鍵: 下一待審頁 (非輸入焦點時)
        elif key == QtCore.Qt.Key.Key_N and not self.text_editor.hasFocus():
            self.next_pending_page()
            event.accept()
            return
        super().keyPressEvent(event)

    def reject(self):
        if not self.busy:
            super().reject()

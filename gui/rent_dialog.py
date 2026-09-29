"""Rent a cloud GPU: current Vast offers grouped by region, filterable by GPU class."""

from __future__ import annotations

from .qt import QtCore, QtWidgets

CLASSES = ["All that fit", "RTX PRO 6000", "A100", "H100", "H200", "B200 / B300", "48 GB class"]
MODEL_GB = 34  # q8 weights + 128k context, measured on the instance
REGION_ORDER = ["Europe", "North America", "Asia", "Middle East", "Oceania", "South America", "Africa", "Other"]


class RentDialog(QtWidgets.QDialog):
    def __init__(self, offers: list[dict], home_region: str = "Europe", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rent a cloud GPU")
        self.resize(820, 540)
        self.offers = offers
        self.home = home_region
        self.chosen: dict | None = None

        self.cls = QtWidgets.QComboBox()
        self.cls.addItems(CLASSES)
        self.cls.currentIndexChanged.connect(self._fill)
        self.fast = QtWidgets.QCheckBox("Fast download only (≥ 1 Gb/s: model ready in ~2 min)")
        self.fast.toggled.connect(self._fill)
        filters = QtWidgets.QHBoxLayout()
        filters.addWidget(QtWidgets.QLabel("GPU"))
        filters.addWidget(self.cls)
        filters.addSpacing(12)
        filters.addWidget(self.fast)
        filters.addStretch(1)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["GPU", "VRAM", "$/hour", "Location", "Download", "Reliability"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.itemDoubleClicked.connect(lambda it, _c: self._accept(it))
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._update_button(cur))
        for col, width in enumerate((190, 90, 70, 190, 90, 80)):
            self.tree.setColumnWidth(col, width)

        note = QtWidgets.QLabel(
            f"The model needs about {MODEL_GB} GB. 40 GB cards are marked 'tight'; 48 GB or more is comfortable. "
            "Billing starts when you rent and runs until the instance is destroyed "
            "(Destroy GPU, idle auto-destroy, or when closing FreeCAD).")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Cancel)
        self.rent = buttons.addButton("Rent selected", QtWidgets.QDialogButtonBox.AcceptRole)
        self.rent.setEnabled(False)
        buttons.accepted.connect(lambda: self._accept(self.tree.currentItem()))
        buttons.rejected.connect(self.reject)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addLayout(filters)
        lay.addWidget(self.tree, 1)
        lay.addWidget(note)
        lay.addWidget(buttons)
        self._fill()

    def _visible(self) -> list[dict]:
        cls = self.cls.currentText()
        out = [o for o in self.offers if o["vram_gb"] >= 40]
        if cls != CLASSES[0]:
            out = [o for o in out if o["gpu_class"] == cls]
        if self.fast.isChecked():
            out = [o for o in out if o["down_mbps"] >= 1000]
        return out

    def _fill(self) -> None:
        self.tree.clear()
        offers = self._visible()
        regions = sorted({o["region"] for o in offers},
                         key=lambda r: (r != self.home, REGION_ORDER.index(r) if r in REGION_ORDER else 99))
        first = None
        for region in regions:
            group = sorted((o for o in offers if o["region"] == region), key=lambda o: o["price_h"])
            head = QtWidgets.QTreeWidgetItem([f"{region}  ({len(group)} offers, from ${group[0]['price_h']:.2f}/h)"])
            font = head.font(0)
            font.setBold(True)
            head.setFont(0, font)
            head.setFirstColumnSpanned(True)
            head.setFlags(head.flags() & ~QtCore.Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(head)
            for o in group:
                vram = f"{o['vram_gb']} GB" + (" (tight)" if o["vram_gb"] < 48 else "")
                row = QtWidgets.QTreeWidgetItem([o["gpu"], vram, f"{o['price_h']:.3f}", o["where"],
                                                 f"{o['down_mbps']:,} Mb/s", f"{o['reliability']:.3f}"])
                row.setData(0, QtCore.Qt.UserRole, o)
                for col in (1, 2, 4, 5):
                    row.setTextAlignment(col, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                head.addChild(row)
                first = first or row
            head.setExpanded(region == self.home or len(regions) <= 2)
            if region == self.home and first is not None:
                self.tree.setCurrentItem(first)
        if not offers:
            self.tree.addTopLevelItem(QtWidgets.QTreeWidgetItem(["No offers match these filters."]))
        self._update_button(self.tree.currentItem())

    def _update_button(self, item) -> None:
        o = item.data(0, QtCore.Qt.UserRole) if item else None
        self.rent.setEnabled(bool(o))
        self.rent.setText(f"Rent {o['gpu']} · ${o['price_h']:.2f}/h" if o else "Rent selected")

    def _accept(self, item) -> None:
        o = item.data(0, QtCore.Qt.UserRole) if item else None
        if o:
            self.chosen = o
            self.accept()

"""First-run wizard: sheet -> provider/password -> template."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QButtonGroup, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QRadioButton, QVBoxLayout, QWidget, QWizard, QWizardPage,
)

from ..config import FullConfig, default_config, write_config
from ..mail import blocks
from ..mail.secrets import store_secret
from ..sheet.reader import SheetError, read_sheet

STARTERS = Path(__file__).resolve().parent.parent / "templates" / "starters"

PROVIDERS = {
    "Gmail / Google Workspace": ("smtp.gmail.com", 587, "imap.gmail.com", 993,
                                 "Use a 16-character App Password (Google Account → Security → "
                                 "2-Step Verification → App passwords). Normal passwords are refused."),
    "Microsoft 365": ("smtp.office365.com", 587, "outlook.office365.com", 993,
                      "Modern auth may block basic SMTP; an app password is required if your "
                      "tenant enforces MFA."),
    "Outlook.com": ("smtp-mail.outlook.com", 587, "outlook.office365.com", 993,
                    "Use an app password if two-step verification is on."),
    "Fastmail": ("smtp.fastmail.com", 587, "imap.fastmail.com", 993,
                 "Create an app password in Settings → Privacy & Security → App passwords."),
    "Zoho": ("smtp.zoho.com", 587, "imap.zoho.com", 993,
             "Use an app-specific password if MFA is enabled."),
    "iCloud": ("smtp.mail.me.com", 587, "imap.mail.me.com", 993,
               "Use an app-specific password from appleid.apple.com."),
    "Other / custom": ("", 587, "", 993, "Enter your provider's SMTP and IMAP hosts."),
}

STARTER_INFO = [
    ("personal_note", "Personal note (recommended)",
     "No image. Plain paragraphs, signed by hand. Best performer for cold outreach."),
    ("promo_card", "Promo card",
     "Heading, hero image, three short paragraphs and one button."),
    ("announcement", "Announcement",
     "Heading, image, two paragraphs, divider and a signature block."),
]


class SheetPage(QWizardPage):
    def __init__(self, wizard: "OnboardingWizard"):
        super().__init__()
        self.wizard_ref = wizard
        self.setTitle("1. Where is your contact list?")
        self.setSubTitle("An .xlsx workbook with a header row. A column named Email is required.")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        content = QWidget()
        content.setMaximumWidth(560)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("C:/path/to/leads.xlsx")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self.info = QLabel("No file selected.")
        self.info.setObjectName("Muted")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch(1)
        wrap = QVBoxLayout()
        wrap.addStretch(1)
        row_wrap = QHBoxLayout()
        row_wrap.addStretch(1)
        row_wrap.addWidget(content)
        row_wrap.addStretch(1)
        wrap.addLayout(row_wrap)
        wrap.addStretch(1)
        outer.addLayout(wrap, 1)
        self.path_edit.textChanged.connect(self._validate)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose contact spreadsheet", "",
                                              "Excel workbooks (*.xlsx *.xlsm)")
        if path:
            self.path_edit.setText(path)
            self._validate()

    def _validate(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            self.info.setText("No file selected.")
            self.completeChanged.emit()
            return
        try:
            data = read_sheet(path)
            self.info.setText(
                f"{data.row_count} contact row(s), {len(data.headers)} column(s), "
                f"tab '{data.tab}'. Columns: " + ", ".join(h for h in data.headers if h)[:180]
            )
        except SheetError as exc:
            self.info.setText(f"⚠ {exc}")
        except Exception as exc:
            self.info.setText(f"⚠ {type(exc).__name__}: {exc}")
        self.completeChanged.emit()

    def isComplete(self) -> bool:  # noqa: N802
        path = self.path_edit.text().strip()
        if not path or not Path(path).exists():
            return False
        try:
            read_sheet(path)
            return True
        except Exception:
            return False


class SenderPage(QWizardPage):
    def __init__(self, wizard: "OnboardingWizard"):
        super().__init__()
        self.wizard_ref = wizard
        self.setTitle("2. Which account sends the mail?")
        self.setSubTitle("One sender account. The password is stored in your OS keyring, never "
                         "in the config file.")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        content = QWidget()
        content.setMaximumWidth(560)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.from_name = QLineEdit("Sam at Samlax")
        self.from_email = QLineEdit("sam@samlax.net")
        self.provider = _combo(list(PROVIDERS))
        self.hint = QLabel(PROVIDERS["Gmail / Google Workspace"][4])
        self.hint.setWordWrap(True)
        self.hint.setObjectName("Muted")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("App password (optional now)")
        form.addRow("From name", self.from_name)
        form.addRow("From email", self.from_email)
        form.addRow("Provider", self.provider)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        layout.addWidget(self.hint)
        note = QLabel("You can add or change the password later in Settings. Nothing is sent "
                      "until you turn off dry run in the config and confirm a send.")
        note.setWordWrap(True)
        note.setObjectName("Muted")
        layout.addWidget(note)
        layout.addStretch(1)
        wrap = QVBoxLayout()
        wrap.addStretch(1)
        row_wrap = QHBoxLayout()
        row_wrap.addStretch(1)
        row_wrap.addWidget(content)
        row_wrap.addStretch(1)
        wrap.addLayout(row_wrap)
        wrap.addStretch(1)
        outer.addLayout(wrap, 1)
        self.provider.currentTextChanged.connect(self._provider_changed)

    def _provider_changed(self, name: str) -> None:
        self.hint.setText(PROVIDERS.get(name, PROVIDERS["Other / custom"])[4])

    def isComplete(self) -> bool:  # noqa: N802
        return bool(self.from_name.text().strip() and "@" in self.from_email.text())


class TemplatePage(QWizardPage):
    def __init__(self, wizard: "OnboardingWizard"):
        super().__init__()
        self.wizard_ref = wizard
        self.setTitle("3. Pick a starting template")
        self.setSubTitle("You can edit every word later in the template builder.")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        content = QWidget()
        content.setMaximumWidth(560)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        self.group = QButtonGroup(self)
        for index, (key, label, description) in enumerate(STARTER_INFO):
            radio = QRadioButton(label)
            radio.setProperty("starter", key)
            if index == 0:
                radio.setChecked(True)
            self.group.addButton(radio, index)
            layout.addWidget(radio)
            caption = QLabel(description)
            caption.setObjectName("Muted")
            caption.setWordWrap(True)
            caption.setContentsMargins(24, 0, 0, 8)
            layout.addWidget(caption)
        layout.addStretch(1)
        wrap = QVBoxLayout()
        wrap.addStretch(1)
        row_wrap = QHBoxLayout()
        row_wrap.addStretch(1)
        row_wrap.addWidget(content)
        row_wrap.addStretch(1)
        wrap.addLayout(row_wrap)
        wrap.addStretch(1)
        outer.addLayout(wrap, 1)

    def starter(self) -> str:
        button = self.group.checkedButton()
        return button.property("starter") if button else "personal_note"


class OnboardingWizard(QWizard):
    def __init__(self, initial_config: FullConfig | None = None,
                 initial_path: Path | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dispatch — set up your campaign")
        self.setWizardStyle(QWizard.ModernStyle)
        self.resize(760, 560)
        self.config: FullConfig | None = None
        self.config_path: Path | None = None

        self.sheet_page = SheetPage(self)
        self.sender_page = SenderPage(self)
        self.template_page = TemplatePage(self)
        self.addPage(self.sheet_page)
        self.addPage(self.sender_page)
        self.addPage(self.template_page)
        self.setButtonText(QWizard.FinishButton, "Create campaign")

        if initial_config:
            self.sheet_page.path_edit.setText(str(initial_config.sheet_file))
            self.sender_page.from_name.setText(initial_config.sender.from_name)
            self.sender_page.from_email.setText(initial_config.sender.from_email)
            self.sender_page.provider.setCurrentText(
                next((k for k, v in PROVIDERS.items()
                      if v[0] == initial_config.sender.smtp.host),
                     "Other / custom")
            )
        self.sheet_page._validate()

    def accept(self) -> None:  # noqa: D401
        sheet = Path(self.sheet_page.path_edit.text().strip()).expanduser().resolve()
        base_dir = sheet.parent
        cfg = default_config(base_dir)
        cfg.campaign.sheet_path = str(sheet)
        cfg.campaign.sheet_tab = None
        cfg.campaign.date_order = "DMY"

        cfg.sender.from_name = self.sender_page.from_name.text().strip() or "Sender"
        cfg.sender.from_email = self.sender_page.from_email.text().strip()
        cfg.sender.reply_to = cfg.sender.from_email
        provider = self.sender_page.provider.currentText()
        smtp_host, smtp_port, imap_host, imap_port, _ = PROVIDERS[provider]
        if smtp_host:
            cfg.sender.smtp.host = smtp_host
            cfg.sender.smtp.port = smtp_port
            cfg.sender.smtp.starttls = True
            cfg.sender.imap.host = imap_host
            cfg.sender.imap.port = imap_port
            cfg.sender.imap.ssl = True
        cfg.sender.secret_ref = f"keyring:dispatch/{cfg.sender.id}"

        starter = self.template_page.starter()
        doc = blocks.load_document(STARTERS / f"{starter}.json")
        name = "promo" if starter != "personal_note" else "personal_note"
        doc["name"] = doc.get("name") or name
        tdir = base_dir / "templates"
        tdir.mkdir(parents=True, exist_ok=True)
        json_path = tdir / f"{name}.json"
        blocks.save_document(doc, json_path)
        html = blocks.render_document_html(doc)
        text = blocks.render_document_text(doc)
        (tdir / f"{name}.html").write_text(html, encoding="utf-8")
        (tdir / f"{name}.txt").write_text(text, encoding="utf-8")
        cfg.template.kind = "blocks"
        cfg.template.blocks_path = str(json_path)
        cfg.template.html_path = str(tdir / f"{name}.html")
        cfg.template.text_path = str(tdir / f"{name}.txt")
        cfg.template.subject = str(doc.get("subject") or cfg.template.subject)
        cfg.template.footer.postal_address = (
            f"{cfg.sender.from_name}, 1 Example Street, London, E1 6AN"
        )

        cfg_path = base_dir / "campaign.yaml"
        cfg_path = write_config(cfg, cfg_path)

        password = self.sender_page.password.text()
        if password:
            try:
                store_secret(cfg.sender.secret_ref, password)
            except Exception as exc:
                QMessageBox.warning(
                    self, "Password not saved",
                    f"Could not store the password in the OS keyring:\n{exc}\n\n"
                    "You can set it later in Settings.",
                )

        self.config = cfg
        self.config_path = cfg_path
        super().accept()

    def reject(self) -> None:
        self.config = None
        super().reject()


from PySide6.QtWidgets import QComboBox  # noqa: E402


def _combo(items: list[str]) -> QComboBox:
    box = QComboBox()
    box.addItems(items)
    return box

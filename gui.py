#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI (프론트엔드)
- 사용자가 브라우저에서 직접 로그인 후, JSESSIONID 등 쿠키 값을 입력/확정
- 날짜/시간(2시간 블록)/코트번호/실행시각을 입력하여 N개의 예약을 추가
- 실행 시작을 누르면 server.ReservationManager 스케줄러가 백그라운드에서
  실행시각 도달 시 reserv.sh 를 순차 실행
"""
import sys
import os
# Disable Chromium sandbox early (must be set before importing PySide6 WebEngine)
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
import hmac
import hashlib
from datetime import datetime
from PySide6.QtCore import Qt, QDate, QDateTime, Signal, QObject, QUrl
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QDateEdit, QDateTimeEdit, QComboBox,
    QSpinBox, QTableWidget, QTableWidgetItem, QMessageBox, QTextEdit, QInputDialog
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage

# 같은 폴더의 scheduler.py 모듈 사용
import scheduler

# ----- Secret as a constant (no file management) -----
# Replace the value below with your secret string.
APP_SECRET = "a4d6fef01e194c9b81a7c6151d447e0f"


class LogBridge(QObject):
    """ server.ReservationManager의 로그 콜백을 Qt 시그널로 브릿지 """
    logSignal = Signal(str)

    def emit(self, text: str):
        self.logSignal.emit(text)


TIME_SLOTS = [
    ("06:00", "08:00"),
    ("08:00", "10:00"),
    ("10:00", "12:00"),
    ("12:00", "14:00"),
    ("14:00", "16:00"),
    ("16:00", "18:00"),
    ("18:00", "20:00"),
    ("20:00", "22:00"),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tennis Reservation GUI")
        self.resize(900, 650)

        # ----- Widgets -----
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)

        # 로그인 브라우저 영역 (사용자 직접 로그인)
        topRow = QHBoxLayout()
        self.btnOpenLogin = QPushButton("로그인 창 열기")
        topRow.addWidget(self.btnOpenLogin)
        vbox.addLayout(topRow)

        # 내장 웹뷰 (초기에는 숨김) — 사용자가 직접 로그인하면 쿠키 자동 수집
        self.webProfile = QWebEngineProfile("login-profile", self)
        self.webPage = QWebEnginePage(self.webProfile, self)
        self.webView = QWebEngineView(self)
        self.webView.setPage(self.webPage)
        self.webView.setVisible(False)
        vbox.addWidget(self.webView, stretch=2)

        # 로그인/쿠키 영역
        cookieBox = QHBoxLayout()
        cookieBox.addWidget(QLabel("로그인 후 Cookie(JSESSIONID=...) (자동 감지됨):"))
        self.cookieEdit = QLineEdit()
        self.cookieEdit.setPlaceholderText("예: JSESSIONID=XXXX.worker2")
        cookieBox.addWidget(self.cookieEdit, stretch=1)
        self.btnCookieSet = QPushButton("로그인 완료 / 쿠키 확정")
        cookieBox.addWidget(self.btnCookieSet)
        vbox.addLayout(cookieBox)

        # 예약 입력 영역
        formRow1 = QHBoxLayout()
        formRow1.addWidget(QLabel("날짜"))
        self.dateEdit = QDateEdit()
        self.dateEdit.setCalendarPopup(True)
        self.dateEdit.setDate(QDate.currentDate())
        formRow1.addWidget(self.dateEdit)

        formRow1.addWidget(QLabel("시간(2시간 블록)"))
        self.timeCombo = QComboBox()
        for fr, to in TIME_SLOTS:
            self.timeCombo.addItem(f"{fr} - {to}", (fr, to))
        formRow1.addWidget(self.timeCombo)

        formRow1.addWidget(QLabel("코트번호"))
        self.courtSpin = QSpinBox()
        self.courtSpin.setRange(1, 99)  # 필요에 맞게 상한 조정
        formRow1.addWidget(self.courtSpin)

        # TIME CODE Base (선택 입력)
        formRow1.addWidget(QLabel("Time Base(선택)"))
        self.timeBaseEdit = QLineEdit()
        self.timeBaseEdit.setPlaceholderText("예: 69 (비우면 자동)")
        self.timeBaseEdit.setFixedWidth(120)
        formRow1.addWidget(self.timeBaseEdit)

        vbox.addLayout(formRow1)

        # 실행 시각 (예약 실행을 실제로 트리거할 시간)
        formRow2 = QHBoxLayout()
        formRow2.addWidget(QLabel("프로그램 실행 시각"))
        self.execAtEdit = QDateTimeEdit()
        self.execAtEdit.setCalendarPopup(True)
        self.execAtEdit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self.execAtEdit.setDateTime(QDateTime.currentDateTime())
        formRow2.addWidget(self.execAtEdit, stretch=1)

        self.btnAdd = QPushButton("예약 추가")
        formRow2.addWidget(self.btnAdd)
        self.btnDelete = QPushButton("예약 삭제")
        formRow2.addWidget(self.btnDelete)

        self.btnStart = QPushButton("실행 시작(스케줄러 가동)")
        formRow2.addWidget(self.btnStart)

        vbox.addLayout(formRow2)

        # 예약 리스트 테이블
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["reservDate", "fromTime", "toTime", "timeCode", "courtNo", "execAt"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        vbox.addWidget(self.table, stretch=1)

        # 로그 영역
        vbox.addWidget(QLabel("로그"))
        self.logText = QTextEdit()
        self.logText.setReadOnly(True)
        vbox.addWidget(self.logText, stretch=1)

        # ----- Server manager -----
        self.logBridge = LogBridge()
        self.manager = scheduler.ReservationManager(log_callback=self.logBridge.emit)

        # 버튼/쿠키 스토어 연결
        self.btnOpenLogin.clicked.connect(self.onOpenLogin)
        self.webProfile.cookieStore().cookieAdded.connect(self.onCookieAdded)

        # ----- Signals -----
        self.btnCookieSet.clicked.connect(self.onCookieSet)
        self.cookieEdit.setToolTip("내장 로그인 창에서 자동으로 채워집니다. 수동 입력도 가능")
        self.btnAdd.clicked.connect(self.onAddReservation)
        self.btnStart.clicked.connect(self.onStart)
        self.btnDelete.clicked.connect(self.onDeleteReservation)
        self.logBridge.logSignal.connect(self.appendLog)

    def appendLog(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logText.append(f"[{ts}] {text}")

    def onOpenLogin(self):
        # 로그인 페이지를 내장 브라우저로 열어 사용자에게 직접 로그인하게 함
        if not self.webView.isVisible():
            self.webView.setVisible(True)
            self.webView.resize(self.width(), int(self.height() * 0.5))
        # 최초 접속 도메인으로 진입
        self.webView.load(QUrl("https://reserve.gmuc.co.kr/"))
        self.appendLog("로그인 창을 열었습니다. 로그인 완료 후 자동으로 쿠키를 감지합니다.")

    def onCookieAdded(self, cookie):
        try:
            name = bytes(cookie.name()).decode("utf-8", errors="ignore")
            domain = cookie.domain()
            if name == "JSESSIONID" and ("gmuc.co.kr" in domain):
                value = bytes(cookie.value()).decode("utf-8", errors="ignore")
                cookie_str = f"JSESSIONID={value}"
                self.manager.set_cookie(cookie_str)
                self.cookieEdit.setText(cookie_str)
                self.appendLog(f"자동 쿠키 감지 완료: {cookie_str}")
        except Exception as e:
            self.appendLog(f"쿠키 감지 중 오류: {e}")

    def onCookieSet(self):
        cookie = self.cookieEdit.text().strip()
        if not cookie or "JSESSIONID=" not in cookie:
            QMessageBox.warning(self, "쿠키 확인", "JSESSIONID=... 형태로 입력해 주세요.")
            return
        self.manager.set_cookie(cookie)
        self.appendLog(f"쿠키 설정 완료: {cookie}")

    def onAddReservation(self):
        if not self.manager.cookie:
            QMessageBox.warning(self, "쿠키 필요", "먼저 로그인 후 쿠키를 설정하세요.")
            return

        qdate = self.dateEdit.date()
        reservDate = qdate.toString("yyyyMMdd")
        fromTime, toTime = self.timeCombo.currentData()
        courtNo = int(self.courtSpin.value())

        execAt = self.execAtEdit.dateTime().toPython()  # datetime

        # 서버의 규칙 해석 (timeCode, courtCode 포함)
        # time base override 파싱(비우면 None)
        timeBaseOverride = None
        tb_text = self.timeBaseEdit.text().strip()
        if tb_text:
            if not tb_text.isdigit():
                QMessageBox.warning(self, "Time Base", "Time Base는 숫자여야 합니다(예: 69). 비워두면 자동 계산됩니다.")
                return
            timeBaseOverride = int(tb_text)

        r = self.manager.create_reservation(
            reservDate=reservDate,
            fromTime=fromTime,
            toTime=toTime,
            courtNo=courtNo,
            exec_at=execAt,
            timeBaseOverride=timeBaseOverride,
        )

        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(r.reservDate))
        self.table.setItem(row, 1, QTableWidgetItem(r.fromTime))
        self.table.setItem(row, 2, QTableWidgetItem(r.toTime))
        self.table.setItem(row, 3, QTableWidgetItem(r.timeCode))
        self.table.setItem(row, 4, QTableWidgetItem(str(r.courtNo)))
        self.table.setItem(row, 5, QTableWidgetItem(execAt.strftime("%Y-%m-%d %H:%M:%S")))
        # 행에 Reservation id를 저장 (첫 셀의 UserRole 사용)
        first_item = self.table.item(row, 0)
        if first_item is None:
            first_item = QTableWidgetItem(r.reservDate)
            self.table.setItem(row, 0, first_item)
        first_item.setData(Qt.UserRole, r.id)

        self.appendLog(
            f"예약 추가: date={r.reservDate}, {r.fromTime}-{r.toTime}, courtNo={r.courtNo} "
            f"(timeCode={r.timeCode}, base={r.timeBase}, courtCode={r.courtCode}), 실행시각={execAt}"
        )

    def onDeleteReservation(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "선택 필요", "삭제할 예약 행을 선택하세요.")
            return
        item = self.table.item(row, 0)
        res_id = None
        if item is not None:
            res_id = item.data(Qt.UserRole)
        # 예약 매니저에서 취소 시도
        if res_id is not None:
            ok = self.manager.cancel_reservation(int(res_id))
            if ok:
                self.appendLog(f"예약 취소: id={res_id}")
            else:
                self.appendLog(f"예약 취소 실패(목록/큐에서 찾지 못함): id={res_id}")
        else:
            self.appendLog("선택 행에 예약 id 정보가 없어 UI 행만 삭제합니다.")
        # 테이블에서 행 삭제
        self.table.removeRow(row)

    def onStart(self):
        if not self.manager.cookie:
            QMessageBox.warning(self, "쿠키 필요", "먼저 로그인 후 쿠키를 설정하세요.")
            return
        self.manager.start()
        self.appendLog("스케줄러 가동 시작 (예약들을 실행시각에 맞춰 순차 실행)")

def main():
    app = QApplication(sys.argv)

    # ----- Access gate: daily HMAC code -----
    # Use a constant for the secret (no file/env fallback per request)
    secret = APP_SECRET

    today = datetime.now().strftime("%Y%m%d")
    expected = hmac.new(secret.encode("utf-8"), today.encode("utf-8"), hashlib.sha256).hexdigest()

    ok = False
    for _ in range(3):
        code, accepted = QInputDialog.getText(
            None,
            "접속 코드 확인",
            f"오늘의 코드 입력:",
        )
        if not accepted:
            sys.exit(1)
        if code.strip().lower() == expected:
            ok = True
            break
        QMessageBox.warning(None, "코드 불일치", "코드가 올바르지 않습니다. 다시 시도하세요.")

    if not ok:
        QMessageBox.critical(None, "접근 거부", "코드 검증 실패. 프로그램을 종료합니다.")
        sys.exit(1)

    # ----- Proceed to main UI -----
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()

# spice-lab-mcp (한국어)

Windows에서 **Cadence OrCAD Capture / PSpice A/D**를 AI 에이전트(Claude Code, Codex 등 MCP 클라이언트)가
조작할 수 있게 하는 로컬 MCP 서버입니다. 회로도 작성, 실제 시뮬레이션 실행, Probe 마커 표시,
증빙 스크린샷 저장을 지원하며, 임의 스크립트 실행이나 Cadence 외 프로그램 조작은 막혀 있습니다.

> **Cadence와 무관한 개인 프로젝트입니다.** OrCAD, PSpice, Cadence는 Cadence Design Systems, Inc.의 상표입니다.
> 이 저장소에는 Cadence 소프트웨어나 라이브러리가 **포함되어 있지 않으며**, 사용자가 직접 설치·라이선스한
> 프로그램을 자동화합니다. 상업적으로 자동화를 사용하기 전에 Cadence 사용권 계약(특히 Lite/학생판)을 확인하세요.

## 설치

```powershell
cd spice-lab-mcp
powershell -ExecutionPolicy Bypass -File .\install.ps1            # 가상환경 생성 + 등록 명령 출력
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Register  # Claude Code 등록까지
```

Cadence 경로는 `CADENCE_BIN`, `CDSROOT`, `C:\Cadence\SPB_*\tools\bin` 순서로 자동 탐지합니다.
읽기 허용 폴더·제한 시간 등을 바꾸려면 `config.example.json`을 `config.json`으로 복사해 수정하세요.
등록 후 새 세션을 시작해야 도구가 보입니다.

## 주요 기능

- **JSON 사양으로 회로도 작성** — 허용된 부품(R, C, L, D1N4148, VAC, VDC, VSIN, uA741)·0.1 inch 격자 좌표·
  넷 이름만 받아 Capture Tcl을 생성하고, Capture가 실제로 만든 netlist와 선언한 연결을 비교 검증합니다.
  불일치가 있으면 시뮬레이션을 거절합니다.
- **좌표 없이 Probe 제어** — 메뉴를 라벨 경로로 찾아 명령 ID로 실행하고, 대화상자는 컨트롤 ID로 채웁니다.
  `search forward level(max-3)`, `search forward min` 같은 검색으로 창 크기와 무관하게 정확한 Mark를 찍습니다.
- **보고서용 캡처** — `set_window_layout`으로 창 크기를 고정해 Probe 라벨이 문서에서도 읽히게 하고,
  `save_screenshot(crop=...)`으로 원본 픽셀 그대로 필요한 영역만 저장합니다.

사용 예시, 안전 모델, 테스트 방법, 알려진 제한은 [README.md](README.md)를 참고하세요.

## 주의할 점

- Probe는 max/min을 **화면에 보이는 X 범위에서만** 계산합니다. 마커를 먼저 찍고 축을 확대하세요(기본 동작).
- R/C를 90° 회전하면 1번 핀이 **아래쪽**입니다. uA741 전원 핀 끝점은 (x+0.3, y+0), (x+0.3, y+0.6)입니다.
- Capture를 강제 종료했다면 다음 실행 때 복구 확인 대화상자가 생성 스크립트를 멈춥니다. 응답 후 새 빌드를 준비하세요.
- 검증 환경은 Capture/PSpice 17.2 Lite입니다. 다른 버전은 테스트를 먼저 실행해 확인하세요.
- 수업 과제에 사용할 때는 소속 학교의 명예규율·AI 사용 지침을 따르세요.

## 라이선스

Copyright (C) 2026 Jang cuber

GNU General Public License 3판 또는 (선택에 따라) 그 이후 판(`GPL-3.0-or-later`)으로 배포합니다.
누구나 자유롭게 사용·수정·재배포할 수 있지만, 수정본을 배포할 때는 같은 GPL로 소스 코드와 함께 공개해야 합니다.
보증은 제공하지 않습니다. 전문은 [LICENSE](LICENSE)를 참고하세요.

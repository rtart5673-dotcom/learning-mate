# 📚 AI 러닝메이트

강의자료 PDF를 대단원/소단원으로 나누고, 테스트를 통과해야 다음 단원이 열리는 공부 앱.

## 기능
- PDF → 대단원/소단원 자동 분류 (긴 교재, 스캔본, 그림·수식 PDF 지원)
- 소단원 테스트: 객관식 + 서술형(AI 채점), 전부 맞혀야 통과
- 대단원 종합 테스트: 소단원 내용을 섞어서, 80% 이상이면 통과
- 오답노트: 개념별로 묶어서 많이 틀린 순서, 2번 이상 틀리면 ⭐ 중요
- 힌트: 2번 이상 틀리면 3단계 힌트
- 오늘의 복습: 틀린 개념을 1 → 3 → 7 → 14 → 30일 간격으로 다시 출제
- 진도 자동 저장, 백업 받기/불러오기

## 내 컴퓨터(또는 Codespaces)에서 실행
```
pip install -r requirements.txt
streamlit run app.py
```

## 웹에 배포하기 (Streamlit Community Cloud, 무료)
1. 이 폴더를 GitHub 저장소에 올린다 (`saves/` 폴더는 올리지 않음)
2. share.streamlit.io 에 GitHub 계정으로 로그인 → **New app** → 저장소와 `app.py` 선택
3. **Advanced settings → Secrets** 에 아래처럼 입력하고 Deploy

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
APP_PASSWORD = "친구들한테만 알려줄 비밀번호"
```

### 배포할 때 주의
- API 비용은 키 주인(나)에게 나온다. `APP_PASSWORD`를 꼭 설정할 것
- Streamlit Cloud는 앱이 재시작되면 저장 파일이 사라질 수 있다.
  각자 **💾 진도 백업 받기**로 저장해 두고 **📂 백업 불러오기**로 복원
- 사용자는 닉네임으로 구분된다 (로그인 기능은 아님)

## 설정 바꾸기
`app.py` 맨 위 설정값에서 바꿀 수 있다.

| 설정 | 기본값 | 뜻 |
|---|---|---|
| `MODEL` | claude-sonnet-5-5 | 사용할 Claude 모델 |
| `IMPORTANT_AT` | 2 | 몇 번 틀리면 ⭐ 중요 |
| `HINT_AT` | 2 | 몇 번 틀리면 힌트 |
| `FINAL_PASS` | 0.8 | 종합 테스트 통과 기준 |
| `INTERVALS` | 1,3,7,14,30 | 복습 간격(일) |
| `MAX_DOC_PAGES` | 40 | 한 번에 AI에 보내는 최대 페이지 |

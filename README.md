### 📌 스트림 제어 함수  
`_begin_stream_cancel(idx)` - 특정 채팅의 스트림 취소 이벤트 생성 및 등록  
`_end_stream_cancel(idx)` - 스트림 취소 이벤트 제거  
`_should_cancel(idx)` - 특정 채팅의 취소 여부 확인  
`stop_generation(idx)` - 특정 채팅의 스트림 중지 신호 전송  
`stop_all_generation()` - 모든 활성 채팅 스트림 중지  

### 🔧 설정 및 초기화 함수  
`_build_openrouter_client()` - OpenRouter API 클라이언트 생성  
`_warm_openrouter()` - 첫 메시지 지연 감소를 위한 API 워밍업 요청  
`_load_model_choices()` - 사용 가능한 모델 목록 로드  
`_apply_openrouter_settings(api_key, api_base)` - API 자격증명 저장 및 모델 목록 새로고침  
`update_openrouter_settings(...)` - 설정 탭에서 자격증명 업데이트 콜백  

### 💬 메시지 생성 함수  
`_prepare_chat_messages(message, history, instruction)` - 채팅 히스토리를 API 형식으로 변환  
`_generate_openrouter_reply(...)` - OpenRouter API로 일반 응답 생성  
`_stream_openrouter_reply(...)` - OpenRouter API로 스트리밍 응답 생성  
`_stream_basic_reply(...)` - 폴백용 기본 응답 생성  
`generate_reply_stream(...)` - 스트리밍 응답 반환 (메인 인터페이스)  
`generate_reply(...)` - 일반 응답 반환 (메인 인터페이스)  

### 🎯 메시지 전송 및 처리    
`_pump_stream(idx, iterator, out_q, stop_event)` - 백그라운드 스레드에서 스트림 소비하여 큐에 전송  
`dispatch_message(...)` - 메시지를 하나 또는 모든 채팅에 전송 (스트리밍)  
`send_or_stop(...)` - Send/Stop 버튼 통합 핸들러 (스트리밍 상태 관리)  

### 🧹 채팅 관리 함수  
`add_chat(visible_flags)` - 새 채팅 세션 추가 및 압축  
`close_chat(index, visible_flags, ...)` - 특정 채팅 닫고 나머지 왼쪽으로 압축  
`dispatch_clear(...)` - 채팅 히스토리 지우기 (개별 또는 전체)  
`toggle_sync(sync_enabled)` - 동기화 모드 토글 (바인드/언바인드)   

### 📝 로그 및 피드백  
`_sanitize_log_entries(entries)` - 로그 데이터를 CSV 형식으로 정규화  
`_handle_feedback_event(event, feedback_store, log_entries, chat_index)` - 좋아요/싫어요 피드백 처리 및 로그 업데이트  
`download_logs(log_entries)` - 로그를 CSV 파일로 직렬화하여 다운로드  

### 🔄 세션 관리  
`_reconstruct_session_from_logs(logs)` - 저장된 로그에서 채팅 히스토리 및 상태 복원  
`load_cached_session(cached_logs)` - 캐시된 로그로 UI 상태 복원   

### 🎨 UI 헬퍼 함수  
`_history_to_messages(history)` - 튜플 기반 히스토리를 Chatbot 메시지 딕셔너리로 변환  
`_append_stopped_tag(a_text)` - 어시스턴트 텍스트에 '[stopped]' 태그 추가  
`_inject_latency(a_text, ms)` - 응답 지연시간 배지 추가  
`_sanitize_temperature(value)` - 온도 값을 0-1 범위로 제한  
`_sanitize_model(value)` - 모델 선택값 검증 및 기본값 설정  

### 🔀 설정 전파 함수  
`propagate_system(value, visible_flags, origin_index)` - 시스템 프롬프트를 다른 모든 표시된 채팅에 전파  
`propagate_temperature(...)` - 온도 설정을 다른 채팅에 전파  
`propagate_model(...)` - 모델 선택을 다른 채팅에 전파    

### 🏗️ 메인 함수  
`build_demo()` - Gradio UI 전체 조립 (탭, 채팅 섹션, 이벤트 핸들러 등)  

이 애플리케이션은 여러 채팅 세션을 동시에 관리하고, 동기화된 메시지 전송, 실시간 스트리밍, 로그 저장 등을 지원하는 멀티채팅 시스템입니다.
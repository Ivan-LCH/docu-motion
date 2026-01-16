# ===================================================================================================================
# Import 
# ===================================================================================================================

import os
import googleapiclient.discovery
import googleapiclient.errors

from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaFileUpload


# ===================================================================================================================
# Global Variables
# ===================================================================================================================

# 인증 범위 (업로드 권한)
SCOPES = ['https://www.googleapis.com/auth/youtube.upload']



# ===================================================================================================================
# upload shorts
# ===================================================================================================================

def upload_short(file_path, title, description):
    print(f"🚀 유튜브 업로드 시작: {title}")
    
    if not os.path.exists('token.json'):
        print("❌ 인증 토큰(token.json)이 없습니다.")
        return None
        
    creds   = Credentials.from_authorized_user_file('token.json', SCOPES)
    youtube = googleapiclient.discovery.build("youtube", "v3", credentials=creds)
    
    # [Fix] 설명(Description) 안전장치 추가
    # 1. 꺾쇠 괄호 치환 (API 에러 방지)
    safe_description = description.replace("<", "[").replace(">", "]")
    
    # 2. 길이 제한 (유튜브 한도 5000자 -> 안전하게 4500자로 컷)
    if len(safe_description) > 4500:
        print(f"⚠️ 설명 내용이 너무 길어 일부 생략합니다. ({len(safe_description)}자 -> 4500자)")
        safe_description = safe_description[:4500] + "\n\n...(내용이 길어 생략되었습니다. 메일 리포트를 확인하세요.)"

    body = {
        'snippet': {
            'title'       : title[:100], # 제목도 100자 제한
            'description' : safe_description,
            'tags'        : ['주식', '투자', '뉴스', 'AI브리핑', 'Ivan'],
            'categoryId'  : '25' # 뉴스/정치
        },
        'status': {
            'privacyStatus'           : 'public', # 일부 공개
            'selfDeclaredMadeForKids' : False
        }
    }
    
    media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
    
    try:
        request = youtube.videos().insert(
            part       = "snippet,status", 
            body       = body, 
            media_body = media
            )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                print(f"   - 업로드 진행률: {int(status.progress() * 100)}%")
        
        video_id = response['id']
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        print(f"✅ 업로드 완료! URL: {video_url}")
        return video_url
        
    except Exception as e:
        print(f"❌ 업로드 실패: {e}")
        # 에러가 나도 프로그램이 죽지 않도록 None 반환
        # return None
        raise e


# ===================================================================================================================
# End of promgram
# ===================================================================================================================

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Dict, List, Optional
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import cloudinary
import cloudinary.uploader
import json
import os
from dotenv import load_dotenv

load_dotenv()

from database import engine, get_db, Base
from models import User, Group, GroupMember, Message, DirectMessage

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Create all tables
Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://adityawork.live",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Pydantic schemas ---

class LoginRequest(BaseModel):
    token: str  # Google JWT credential


class JoinGroupRequest(BaseModel):
    user_id: int
    group_name: str
    group_type: str  # "city" or "train"


# --- WebSocket connection manager ---

class ConnectionManager:
    def __init__(self):
        # group_id -> list of (websocket, user_id) tuples
        self.active: Dict[int, List[tuple]] = {}

    async def connect(self, ws: WebSocket, group_id: int, user_id: int):
        await ws.accept()
        if group_id not in self.active:
            self.active[group_id] = []
        self.active[group_id].append((ws, user_id))

    def disconnect(self, ws: WebSocket, group_id: int):
        if group_id in self.active:
            self.active[group_id] = [
                (w, u) for w, u in self.active[group_id] if w != ws
            ]

    async def broadcast(self, group_id: int, message: dict):
        if group_id in self.active:
            data = json.dumps(message)
            for ws, _ in self.active[group_id]:
                await ws.send_text(data)


manager = ConnectionManager()


# --- REST endpoints ---

@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    # Verify Google JWT token
    try:
        idinfo = id_token.verify_oauth2_token(
            req.token, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    google_id = idinfo["sub"]
    email = idinfo.get("email", "")
    name = idinfo.get("name", "")
    picture = idinfo.get("picture", "")

    # Only allow IITM DS students
    # if not email.endswith("@ds.study.iitm.ac.in"):
    #     raise HTTPException(status_code=403, detail="Only IITM-BS Students are allowed")

    # Find by google_id or create
    user = db.query(User).filter(User.google_id == google_id).first()
    if not user:
        user = User(google_id=google_id, name=name, email=email, picture=picture)
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Update name/picture in case they changed on Google side
        user.name = name
        user.picture = picture
        db.commit()

    return {"id": user.id, "name": user.name, "email": user.email, "picture": user.picture}
   

@app.post("/join-group")
def join_group(req: JoinGroupRequest, db: Session = Depends(get_db)):
    # Find or create group
    group = db.query(Group).filter(
        Group.name == req.group_name, Group.type == req.group_type
    ).first()
    if not group:
        group = Group(name=req.group_name, type=req.group_type)
        db.add(group)
        db.commit()
        db.refresh(group)

    # Check if already a member
    existing = db.query(GroupMember).filter(
        GroupMember.user_id == req.user_id,
        GroupMember.group_id == group.id,
    ).first()
    if not existing:
        db.add(GroupMember(user_id=req.user_id, group_id=group.id))
        db.commit()

    return {"group_id": group.id, "group_name": group.name, "group_type": group.type}


@app.get("/groups/{user_id}")
def get_user_groups(user_id: int, db: Session = Depends(get_db)):
    memberships = db.query(GroupMember).filter(GroupMember.user_id == user_id).all()
    groups = []
    for m in memberships:
        g = db.query(Group).filter(Group.id == m.group_id).first()
        if g:
            groups.append({"id": g.id, "name": g.name, "type": g.type})
    return groups


@app.get("/group-members/{group_id}")
def get_group_members(group_id: int, db: Session = Depends(get_db)):
    memberships = db.query(GroupMember).filter(GroupMember.group_id == group_id).all()
    members = []
    for m in memberships:
        u = db.query(User).filter(User.id == m.user_id).first()
        if u:
            members.append({"id": u.id, "name": u.name, "email": u.email, "picture": u.picture})
    return members


@app.get("/messages/{group_id}")
def get_messages(group_id: int, db: Session = Depends(get_db)):
    msgs = (
        db.query(Message)
        .filter(Message.group_id == group_id)
        .order_by(Message.timestamp.asc())
        .all()
    )
    result = []
    for m in msgs:
        sender = db.query(User).filter(User.id == m.sender_id).first()
        reply_data = None
        if m.reply_to_id:
            reply_msg = db.query(Message).filter(Message.id == m.reply_to_id).first()
            if reply_msg:
                reply_sender = db.query(User).filter(User.id == reply_msg.sender_id).first()
                reply_data = {
                    "id": reply_msg.id,
                    "sender_name": reply_sender.name if reply_sender else "Unknown",
                    "content": reply_msg.content,
                }
        result.append({
            "id": m.id,
            "sender_name": sender.name if sender else "Unknown",
            "sender_id": m.sender_id,
            "sender_picture": sender.picture if sender else None,
            "content": m.content,
            "file_url": m.file_url,
            "file_type": m.file_type,
            "reply_to": reply_data,
            "timestamp": m.timestamp.isoformat(),
        })
    return result


@app.get("/user/{user_id}")
def get_user_info(user_id: int, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return {"id": u.id, "name": u.name, "email": u.email, "picture": u.picture}


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Max 10 MB.")

    # Determine resource type
    content_type = file.content_type or ""
    resource_type = "image" if content_type.startswith("image/") else "raw"
    file_type = "image" if resource_type == "image" else "file"

    result = cloudinary.uploader.upload(
        contents,
        folder="bsbuddy",
        resource_type=resource_type,
        filename_override=file.filename,
    )
    return {"url": result["secure_url"], "file_type": file_type}


# --- WebSocket endpoint ---

@app.websocket("/ws/{group_id}")
async def websocket_endpoint(ws: WebSocket, group_id: int):
    user_id = int(ws.query_params.get("user_id", 0))
    await manager.connect(ws, group_id, user_id)
    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)
            content = payload.get("content", "")
            reply_to_id = payload.get("reply_to_id")
            file_url = payload.get("file_url")
            file_type = payload.get("file_type")

            db = next(get_db())
            msg = Message(
                group_id=group_id, sender_id=user_id, content=content,
                reply_to_id=reply_to_id, file_url=file_url, file_type=file_type,
            )
            db.add(msg)
            db.commit()
            db.refresh(msg)

            sender = db.query(User).filter(User.id == user_id).first()

            reply_data = None
            if reply_to_id:
                reply_msg = db.query(Message).filter(Message.id == reply_to_id).first()
                if reply_msg:
                    reply_sender = db.query(User).filter(User.id == reply_msg.sender_id).first()
                    reply_data = {
                        "id": reply_msg.id,
                        "sender_name": reply_sender.name if reply_sender else "Unknown",
                        "content": reply_msg.content,
                    }
            db.close()

            await manager.broadcast(group_id, {
                "id": msg.id,
                "sender_name": sender.name if sender else "Unknown",
                "sender_id": user_id,
                "sender_picture": sender.picture if sender else None,
                "content": content,
                "file_url": file_url,
                "file_type": file_type,
                "reply_to": reply_data,
                "timestamp": msg.timestamp.isoformat(),
            })
    except WebSocketDisconnect:
        manager.disconnect(ws, group_id)


# --- User Search & Direct Messages ---

@app.get("/search-users")
def search_users(q: str, db: Session = Depends(get_db)):
    if not q or len(q) < 2:
        return []
    users = db.query(User).filter(
        (User.name.ilike(f"%{q}%")) | (User.email.ilike(f"%{q}%"))
    ).limit(10).all()
    return [{"id": u.id, "name": u.name, "email": u.email, "picture": u.picture} for u in users]


@app.get("/dm-history/{user_id}")
def get_dm_history(user_id: int, db: Session = Depends(get_db)):
    # Get all users this user has DM history with, plus the last message
    from sqlalchemy import or_, and_, desc, func as sqlfunc

    # Find distinct peer user IDs
    sent = db.query(DirectMessage.receiver_id).filter(DirectMessage.sender_id == user_id)
    received = db.query(DirectMessage.sender_id).filter(DirectMessage.receiver_id == user_id)
    peer_ids = set(r[0] for r in sent.union(received).all())

    conversations = []
    for pid in peer_ids:
        # Get last message between the two
        last_msg = (
            db.query(DirectMessage)
            .filter(
                ((DirectMessage.sender_id == user_id) & (DirectMessage.receiver_id == pid)) |
                ((DirectMessage.sender_id == pid) & (DirectMessage.receiver_id == user_id))
            )
            .order_by(DirectMessage.timestamp.desc())
            .first()
        )
        peer = db.query(User).filter(User.id == pid).first()
        if peer and last_msg:
            conversations.append({
                "peer_id": peer.id,
                "peer_name": peer.name,
                "peer_email": peer.email,
                "peer_picture": peer.picture,
                "last_message": last_msg.content if last_msg.content else "(file)",
                "last_timestamp": last_msg.timestamp.isoformat(),
            })

    # Sort by most recent first
    conversations.sort(key=lambda c: c["last_timestamp"], reverse=True)
    return conversations


@app.get("/dm/{user1_id}/{user2_id}")
def get_dm_messages(user1_id: int, user2_id: int, db: Session = Depends(get_db)):
    msgs = (
        db.query(DirectMessage)
        .filter(
            ((DirectMessage.sender_id == user1_id) & (DirectMessage.receiver_id == user2_id)) |
            ((DirectMessage.sender_id == user2_id) & (DirectMessage.receiver_id == user1_id))
        )
        .order_by(DirectMessage.timestamp.asc())
        .all()
    )
    result = []
    for m in msgs:
        sender = db.query(User).filter(User.id == m.sender_id).first()
        reply_data = None
        if m.reply_to_id:
            reply_msg = db.query(DirectMessage).filter(DirectMessage.id == m.reply_to_id).first()
            if reply_msg:
                reply_sender = db.query(User).filter(User.id == reply_msg.sender_id).first()
                reply_data = {
                    "id": reply_msg.id,
                    "sender_name": reply_sender.name if reply_sender else "Unknown",
                    "content": reply_msg.content,
                }
        result.append({
            "id": m.id,
            "sender_name": sender.name if sender else "Unknown",
            "sender_id": m.sender_id,
            "sender_picture": sender.picture if sender else None,
            "content": m.content,
            "file_url": m.file_url,
            "file_type": m.file_type,
            "reply_to": reply_data,
            "timestamp": m.timestamp.isoformat(),
        })
    return result


# DM WebSocket connection manager
class DMConnectionManager:
    def __init__(self):
        # user_id -> list of websockets
        self.active: Dict[int, List[WebSocket]] = {}

    async def connect(self, ws: WebSocket, user_id: int):
        await ws.accept()
        if user_id not in self.active:
            self.active[user_id] = []
        self.active[user_id].append(ws)

    def disconnect(self, ws: WebSocket, user_id: int):
        if user_id in self.active:
            self.active[user_id] = [w for w in self.active[user_id] if w != ws]

    async def send_to_user(self, user_id: int, message: dict):
        if user_id in self.active:
            data = json.dumps(message)
            for ws in self.active[user_id]:
                await ws.send_text(data)


dm_manager = DMConnectionManager()


@app.websocket("/ws/dm/{user_id}")
async def dm_websocket_endpoint(ws: WebSocket, user_id: int):
    await dm_manager.connect(ws, user_id)
    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)
            content = payload.get("content", "")
            receiver_id = payload.get("receiver_id")
            reply_to_id = payload.get("reply_to_id")
            file_url = payload.get("file_url")
            file_type = payload.get("file_type")

            db = next(get_db())
            msg = DirectMessage(
                sender_id=user_id, receiver_id=receiver_id, content=content,
                reply_to_id=reply_to_id, file_url=file_url, file_type=file_type,
            )
            db.add(msg)
            db.commit()
            db.refresh(msg)

            sender = db.query(User).filter(User.id == user_id).first()

            reply_data = None
            if reply_to_id:
                reply_msg = db.query(DirectMessage).filter(DirectMessage.id == reply_to_id).first()
                if reply_msg:
                    reply_sender = db.query(User).filter(User.id == reply_msg.sender_id).first()
                    reply_data = {
                        "id": reply_msg.id,
                        "sender_name": reply_sender.name if reply_sender else "Unknown",
                        "content": reply_msg.content,
                    }
            db.close()

            msg_data = {
                "id": msg.id,
                "sender_name": sender.name if sender else "Unknown",
                "sender_id": user_id,
                "sender_picture": sender.picture if sender else None,
                "content": content,
                "file_url": file_url,
                "file_type": file_type,
                "reply_to": reply_data,
                "timestamp": msg.timestamp.isoformat(),
            }

            await dm_manager.send_to_user(user_id, msg_data)
            await dm_manager.send_to_user(receiver_id, msg_data)
    except WebSocketDisconnect:
        dm_manager.disconnect(ws, user_id)

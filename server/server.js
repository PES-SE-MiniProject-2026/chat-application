const express = require("express");
const http = require("http");
const { Server } = require("socket.io");
const path = require("path");

const app = express();
const server = http.createServer(app);
const io = new Server(server);

app.use(express.static(path.join(__dirname, "../public")));

io.on("connection", (socket) => {
  console.log("User connected:", socket.id);

  socket.on("chat message", (data) => {
    const message = {
      username: String(data.username || "Guest").slice(0, 30),
      text: String(data.text || "").slice(0, 1000),
      time: new Date().toLocaleTimeString()
    };

    if (message.text.trim()) {
      io.emit("chat message", message);
    }
  });

  socket.on("disconnect", () => {
    console.log("User disconnected:", socket.id);
  });
});

const PORT = process.env.PORT || 3000;

server.listen(PORT, () => {
  console.log(`Chat server running at http://localhost:${PORT}`);
});
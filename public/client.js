
const socket = io();

const form = document.getElementById("chat-form");
const username = document.getElementById("username");
const message = document.getElementById("message");
const messages = document.getElementById("messages");

form.addEventListener("submit", (event) => {
  event.preventDefault();

  if (!message.value.trim()) return;

  socket.emit("chat message", {
    username: username.value.trim() || "Guest",
    text: message.value.trim()
  });

  message.value = "";
});

socket.on("chat message", (data) => {
  const item = document.createElement("li");
  const heading = document.createElement("strong");

  heading.textContent = `${data.username} (${data.time}): `;
  item.append(heading, document.createTextNode(data.text));
  messages.appendChild(item);
  messages.scrollTop = messages.scrollHeight;
});

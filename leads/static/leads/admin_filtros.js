(function () {
    "use strict";

    function textoSelecionado(grupo) {
        const selecionado = grupo.querySelector("li.selected a");
        return selecionado ? selecionado.textContent.trim() : "";
    }

    function marcarFiltroAtivo(grupo) {
        const valor = textoSelecionado(grupo);
        const resumo = grupo.querySelector("summary");

        if (!resumo) {
            return;
        }

        const anterior = resumo.querySelector(".vitre-filter-value");
        if (anterior) {
            anterior.remove();
        }

        const ativo = valor && valor !== "Todos" && valor !== "All";
        grupo.classList.toggle("vitre-filter-active", Boolean(ativo));

        if (!ativo) {
            return;
        }

        const indicador = document.createElement("span");
        indicador.className = "vitre-filter-value";
        indicador.textContent = valor;
        resumo.appendChild(indicador);
    }

    function reposicionarFiltros(filtro) {
        const toolbar = document.getElementById("toolbar");

        if (toolbar && toolbar.parentNode) {
            toolbar.insertAdjacentElement("afterend", filtro);
            return;
        }

        const busca = document.getElementById("changelist-search");

        if (busca && busca.parentNode) {
            busca.insertAdjacentElement("afterend", filtro);
        }
    }

    function prepararFiltrosCompactos() {
        const filtro = document.getElementById("changelist-filter");

        if (!filtro) {
            return;
        }

        reposicionarFiltros(filtro);

        const grupos = Array.from(filtro.querySelectorAll("details"));

        grupos.forEach((grupo) => {
            grupo.removeAttribute("open");
            marcarFiltroAtivo(grupo);

            grupo.addEventListener("toggle", () => {
                if (!grupo.open) {
                    return;
                }

                grupos.forEach((outro) => {
                    if (outro !== grupo) {
                        outro.removeAttribute("open");
                    }
                });
            });
        });

        document.addEventListener("click", (evento) => {
            if (filtro.contains(evento.target)) {
                return;
            }

            grupos.forEach((grupo) => {
                grupo.removeAttribute("open");
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", prepararFiltrosCompactos, { once: true });
    } else {
        prepararFiltrosCompactos();
    }
})();

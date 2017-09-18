#!/usr/bin/env python3

import re
import random
import string
import sys

import time
import math
import argparse

from format import read_pair

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
import torch.utils.data

from itertools import takewhile

use_cuda = torch.cuda.is_available()
assert use_cuda

SOS_token = 1
EOS_token = ord('.')

teacher_forcing_ratio = 0.5

MAX_LENGTH=200

class TacticPredictor:
    def __init__(self, output_size, hidden_size):
        self.encoder=EncoderRNN(output_size, hidden_size)
        self.decoder=DecoderRNN(hidden_size, output_size)
        if use_cuda:
            self.encoder = self.encoder.cuda()
            self.decoder = self.decoder.cuda()
        self.vocab_size = output_size

class EncoderRNN(nn.Module):
    def __init__(self, input_size, hidden_size, batch_size, n_layers=3):
        super(EncoderRNN, self).__init__()
        self.cuda()
        self.n_layers = n_layers
        self.hidden_size = hidden_size
        self.batch_size = batch_size
        self.embedding = nn.Embedding(input_size, hidden_size).cuda()
        self.gru = nn.GRU(hidden_size, hidden_size).cuda()

    def forward(self, input, hidden):
        output = self.embedding(input).view(self.batch_size, 1, -1)
        for i in range(self.n_layers):
            output, hidden = self.gru(output, hidden)
        return output, hidden

    def initHidden(self):
        return Variable(torch.zeros(1, 1, self.hidden_size).cuda())

class DecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size, width=1, n_layers=3):
        super(DecoderRNN, self).__init__()
        self.n_layers = n_layers
        self.hidden_size = hidden_size

        self.cuda()

        self.embedding = nn.Embedding(output_size, hidden_size).cuda()
        self.gru = nn.GRU(hidden_size, hidden_size).cuda()
        self.out = nn.Linear(hidden_size, output_size).cuda()
        self.softmax = nn.LogSoftmax().cuda()
        self.k = width

    def forward(self, input, hidden):
        output = self.embedding(input).view(self.k, 1, -1)
        for i in range(self.n_layers):
            output = F.relu(output)
            output, hidden = self.gru(output, hidden)
        output = self.softmax(self.out(output[:,0]))
        return output, hidden

    def initHidden(self):
        result = Variable(torch.zeros(1, 1, self.hidden_size).cuda())

def train(input_variable, target_variable,
          encoder, decoder,
          encoder_optimizer, decoder_optimizer,
          criterion, max_length=MAX_LENGTH):
    encoder_hidden = encoder.initHidden()
    encoder_optimizer.zero_grad()
    decoder_optimizer.zero_grad()

    batch_size = input_variable.size()[0]
    input_length = input_variable.size()[1]
    target_length = target_variable.size()[1]

    encoder_outputs = Variable(torch.zeros(batch_size, max_length, encoder.hidden_size).cuda())

    loss = 0

    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(
            input_variable[:,ei], encoder_hidden)
        encoder_outputs[:,ei] = encoder_output[:,0,:]

    decoder_input = Variable(torch.cuda.LongTensor([[SOS_token]] * batch_size))

    decoder_hidden = encoder_hidden
    use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

    if use_teacher_forcing:
        for di in range(target_length):
            decoder_output, decoder_hidden = decoder(
                decoder_input, decoder_hidden)
            loss += criterion(decoder_output, target_variable[:,di])
            decoder_input = target_variable[:,di]
    else:
        for di in range(target_length):
            decoder_output, decoder_hidden = decoder(
                decoder_input, decoder_hidden)
            topv, topi = decoder_output.data.topk(1)
            ni = topi[:,0]

            decoder_input = Variable(torch.cuda.LongTensor([[c] for c in ni]))

            loss += criterion(decoder_output, target_variable[:,di])
            if all(c == EOS_token for c in ni):
                break;

    loss.backward()

    encoder_optimizer.step()
    decoder_optimizer.step()

    return loss.data[0] / target_length

def asMinutes(s):
    m = math.floor(s / 60)
    s -= m * 60
    return "{}m {:.2f}s".format(m, s)

def timeSince(since, percent):
    now = time.time()
    s = now - since
    es = s / percent
    rs = es - s
    return "{} (- {})".format(asMinutes(s), asMinutes(rs))

def read_text_data(data_path, max_size=None):
    data_set = []
    with open(data_path, mode="r") as data_file:
        pair = read_pair(data_file)
        counter = 0
        while pair and (not max_size or counter < max_size):
            context, tactic = pair
            counter += 1
            context_ids = [ord(x) for x in context]
            tactic_ids = [ord(x) for x in tactic]

            data_set.append([context_ids, tactic_ids])

            pair = read_pair(data_file)
    return data_set

def read_num_data(data_path, max_size=None):
    data_set = []
    with open(data_path, mode="r") as data_file:
        context = data_file.readline()
        counter = 0
        while(context != "" and (not max_size or counter < max_size)):
            tactic = data_file.readline()
            blank_line = data_file.readline()
            assert tactic != "" and (blank_line == "\n" or blank_line == ""), "tactic line: {}\nblank line: {}".format(tactic, blank_line)
            context_ids = [int(num) for num in context.split(" ")]
            tactic_ids = [int(num) for num in tactic.split(" ")]

            data_set.append([context_ids, tactic_ids])
            context = data_file.readline()
    return data_set

def inputFromSentence(sentence):
    if len(sentence) > MAX_LENGTH:
        sentence = sentence[:MAX_LENGTH]
    if len(sentence) < MAX_LENGTH:
        sentence.extend([EOS_token] * (MAX_LENGTH - len(sentence)))
    return sentence

def variableFromSentence(sentence):
    if len(sentence) > MAX_LENGTH:
        sentence = sentence[:MAX_LENGTH]
    if len(sentence) < MAX_LENGTH:
        sentence.extend([EOS_token] * (MAX_LENGTH - len(sentence)))
    sentence = Variable(torch.cuda.LongTensor(sentence).view(-1, 1))
    return sentence

def variablesFromPair(pair):
    return variableFromSentence(pair[0]), variableFromSentence(pair[1])

def variablesFromBatch(batch):
    return (Variable(torch.cuda.LongTensor([context for context, tactic in batch])),
            Variable(torch.cuda.LongTensor([tactic for contex, tactic in batch])))

def commandLinePredict(predictor, numfile, k, max_length):
    predictor.decoder.k = k
    if numfile:
        sentence = sys.stdin.readline()
        tokenlist = [int(w) for w in sentence.split()]
    else:
        sentence = ""
        next_line = sys.stdin.readline()
        while next_line != "+++++\n":
            sentence += next_line
            next_line = sys.stdin.readline()
        tokenlist = [ord(x) for x in sentence]

    tokensresults = predictKTokenlist(predictor, tokenlist, k, max_length)

    if numfile:
        for result in tokensresults:
            print(list(result))
    else:
        for result in tokensresults:
            print(''.join([chr(x) for x in result]))

def predictKTactics(predictor, sentence, beam_width, k, max_length):
    return predictKTokenlist(predictor, [ord(x) for x in sentence],
                             beam_width, max_length)[:k]

def predictKTokenlist(predictor, tokenlist, k, max_length):
    if len(tokenlist) < max_length:
        tokenlist.extend([EOS_token] * (max_length - len(tokenlist)))
    encoder_hidden = encodeContext(predictor.encoder, tokenlist)
    return decodeKTactics(predictor.decoder, encoder_hidden, k, predictor.vocab_size)

def encodeContext(encoder, tokenlist):
    input_variable = variableFromSentence(tokenlist)
    input_length = input_variable.size()[0]
    encoder_hidden = encoder.initHidden()

    encoder_outputs = Variable(torch.zeros(MAX_LENGTH, encoder.hidden_size).cuda())

    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(input_variable[ei],
                                                 encoder_hidden)
        encoder_outputs[ei] = encoder_outputs[ei] + encoder_output[0][0]

    return encoder_hidden

def decodeTactic(decoder, encoder_hidden, vocab_size):
    decoder_hidden = encoder_hidden
    decoded_tokens = []

    decoder_input = Variable(torch.cuda.LongTensor([[SOS_token]]))

    for _ in range(MAX_LENGTH):
        decoder_output, decoder_hidden = decoder(
            decoder_input, decoder_hidden)
        topv, topi = decoder_output.data.topk(1)
        ni = topi[0][0]
        decoded_tokens.append(ni)

        decoder_input = Variable(torch.cuda.LongTensor([[ni]]))

    return decoded_tokens

def trainIters(encoder, decoder, n_epochs, data_pairs, batch_size,
               print_every=100, learning_rate=0.01):
    start = time.time()
    print_loss_total = 0

    encoder_optimizer = optim.SGD(encoder.parameters(), lr=learning_rate)
    decoder_optimizer = optim.SGD(decoder.parameters(), lr=learning_rate)

    input_pairs = [(inputFromSentence(pair[0]), inputFromSentence(pair[1]))
                   for pair in data_pairs]

    dataset = torch.utils.data.TensorDataset(
        torch.LongTensor([inputFromSentence(context)
                               for context, tactic in data_pairs]),
        torch.LongTensor([inputFromSentence(tactic)
                               for context, tactic in data_pairs]))
    loader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=True,
                                         pin_memory=True, num_workers=0)

    criterion = nn.NLLLoss().cuda()

    idx = 0
    n_iters = len(input_pairs) * n_epochs

    print("Starting training.")
    for epoch in range(math.ceil(n_epochs)):
        for context_batch, tactic_batch in loader:
            loss = train(Variable(context_batch).cuda(), Variable(tactic_batch).cuda(),
                         encoder, decoder,
                         encoder_optimizer, decoder_optimizer, criterion)

            print_loss_total += loss

            idx += batch_size
            if idx % print_every == 0:
                print_loss_avg = print_loss_total / print_every
                print_loss_total = 0
                print("{} ({} {:.2f}%) {:.4f}".format(timeSince(start, idx / n_iters),
                                                      idx, idx / n_iters * 100,
                                                      print_loss_avg))

def main():
    global MAX_LENGTH
    parser = argparse.ArgumentParser(description=
                                     "pytorch model for proverbot")
    parser.add_argument("--nepochs", default=15, type=float)
    parser.add_argument("--save", default=None, required=True)
    parser.add_argument("--train", default=False, const=True,
                        action='store_const')
    parser.add_argument("--scrapefile", default="scrape.txt")
    parser.add_argument("--numfile", default=False, const=True,
                        action='store_const')
    parser.add_argument("--vocabsize", default=128, type=int)
    parser.add_argument("--batchsize", default=32, type=int)
    parser.add_argument("--maxlength", default=200, type=int)
    parser.add_argument("--printevery", default=10, type=int)
    parser.add_argument("--numpredictions", default=3, type=int)
    args = parser.parse_args()
    hidden_size = args.vocabsize * 2
    output_size = args.vocabsize
    MAX_LENGTH = args.maxlength
    if args.train:
        if args.numfile:
            data_set = read_num_data(args.scrapefile)
        else:
            data_set = read_text_data(args.scrapefile)
        print("Initializing CUDA...")
        decoder = DecoderRNN(hidden_size, output_size, args.batchsize).cuda()
        encoder = EncoderRNN(output_size, hidden_size, args.batchsize).cuda()
        trainIters(encoder, decoder, args.nepochs,
                   data_set, args.batchsize, print_every=args.printevery)
        with open(args.save + ".enc", "wb") as f:
            torch.save(encoder.state_dict(), f)
        with open(args.save + ".dec", "wb") as f:
            torch.save(decoder.state_dict(), f)
    else:
        predictor = loadPredictor(args.save, output_size, hidden_size)
        commandLinePredict(predictor, args.numfile, args.numpredictions, args.maxlength)

def loadPredictor(path_stem, output_size, hidden_size):
    predictor = TacticPredictor(output_size, hidden_size)
    predictor.encoder.load_state_dict(torch.load(path_stem + ".enc"))
    predictor.decoder.load_state_dict(torch.load(path_stem + ".dec"))
    return predictor

# The code below here was copied from
# https://ibm.github.io/pytorch-seq2seq/public/_modules/seq2seq/models/TopKDecoder.html
# and modified. This code is available under the apache license.
def decodeKTactics(decoder, encoder_hidden, k, v):
    pos_index = Variable(torch.cuda.LongTensor([0]) * k).view(-1, 1)

    hidden = _inflate(encoder_hidden, k)

    sequence_scores = torch.cuda.Tensor(k, 1)
    sequence_scores.fill_(-float('Inf'))
    sequence_scores.index_fill_(0, torch.cuda.LongTensor([0]), 0.0)
    sequence_scores = Variable(sequence_scores)

    input_var = Variable(torch.cuda.LongTensor([[SOS_token]*k]))

    stored_predecessors = list()
    stored_emitted_symbols = list()

    for j in range(MAX_LENGTH):
        decoder_output, hidden = decoder(input_var, hidden)

        sequence_scores = _inflate(sequence_scores, v)
        sequence_scores += decoder_output

        scores, candidates = sequence_scores.view(1, -1).topk(k)

        input_var = (candidates % v).view(1, k)
        sequence_scores = scores.view(k, 1)

        predecessors = (candidates / v +
                        pos_index.expand_as(candidates)).view(k, 1)
        hidden = hidden.index_select(1, predecessors.squeeze())

        eos_indices = input_var.data.eq(EOS_token)
        if eos_indices.nonzero().dim() > 0:
            sequence_scores.data.masked_fill_(torch.transpose(eos_indices, 0, 1),
                                              -float('inf'))

        stored_predecessors.append(predecessors)
        stored_emitted_symbols.append(torch.transpose(input_var, 0, 1))


    # Trace back from the final three highest scores
    _, next_idxs = sequence_scores.view(k).sort(descending=True)
    seqs = []
    eos_found = 0
    for i in range(MAX_LENGTH - 1, -1, -1):
        # The next column of symbols from the end
        next_symbols = stored_emitted_symbols[i].view(k).index_select(0, next_idxs).data
        # The predecessors of that column
        next_idxs = stored_predecessors[i].view(k).index_select(0, next_idxs)

        # Handle sequences that ended early
        eos_indices = stored_emitted_symbols[i].data.squeeze(1).eq(EOS_token).nonzero()
        if eos_indices.dim() > 0:
            for j in range(eos_indices.size(0)-1, -1, -1):
                idx = eos_indices[j]

                res_k_idx = k - (eos_found % k) - 1
                eos_found += 1
                res_idx = res_k_idx

                next_idxs[res_idx] = stored_predecessors[i][idx[0]]
                next_symbols[res_idx] = stored_emitted_symbols[i][idx[0]].data[0]

        # Commit the result
        seqs.insert(0, next_symbols)

    # Transpose
    seqs = [[data[i] for data in seqs] for i in range(k)]
    # Cut off EOS tokens
    seqs = [list(takewhile(lambda x: x != EOS_token, seq)) for seq in seqs]

    return seqs

def _inflate(tensor, times):
    tensor_dim = len(tensor.size())
    if tensor_dim == 3:
        b = tensor.size(1)
        return tensor.repeat(1, 1, times).view(tensor.size(0), b * times, -1)
    elif tensor_dim == 2:
        return tensor.repeat(1, times)
    elif tensor_dim == 1:
        b = tensor.size(0)
        return tensor.repeat(times).view(b, -1)
    else:
        raise ValueError("Tensor can be of 1D, 2D, or 3D only. "
                         "This one is {}D.".format(tensor_dim))

def _mask_symbol_scores(self, score, idx, masking_score=-float('inf')):
    score[idx] = masking_score

def _mask(tensor, idx, dim=0, masking_score=-float('inf')):
    if len(idx.size()) > 0:
        indices = idx[:,0]
        tensor.index_fill_(dim, indices, masking_score)

## ENDING HERE

if __name__ == "__main__":
    main()
